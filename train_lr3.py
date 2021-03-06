import torch
import torch.nn as nn
from torch import optim
from torch.utils.data import DataLoader
from torch.distributions.normal import Normal
from torch.cuda.amp import GradScaler, autocast
from args_lr3 import parse_args
from data import KORDataset, collate_fn_tr, collate_fn_synth
from hps import Hyperparameters
from model import SmartVocoder
from utils_lr3 import actnorm_init, get_logger, mkdir, stft
import numpy as np
import librosa
import os
import time
import datetime
import json
import gc

torch.backends.cudnn.benchmark = True
np.set_printoptions(precision=4)


def load_dataset(args):
    train_dataset = KORDataset(args.data_path, True, 0.1)
    test_dataset = KORDataset(args.data_path, False, 0.1)

    collate_fn1 = lambda batch: collate_fn_tr(batch, args.max_time_steps, args.hop_length)
    collate_fn2 = lambda batch: collate_fn_synth(batch, args.hop_length)

    train_loader = DataLoader(train_dataset, batch_size=args.bsz, shuffle=True, collate_fn=collate_fn1,
                              num_workers=args.num_workers, pin_memory=True)
    test_loader = DataLoader(test_dataset, batch_size=args.bsz, collate_fn=collate_fn1,
                             num_workers=args.num_workers, pin_memory=True)
    synth_loader = DataLoader(test_dataset, batch_size=1, collate_fn=collate_fn2,
                              num_workers=args.num_workers, pin_memory=True)

    print('num of train samples', len(train_loader))
    print('num of test samples', len(test_loader))

    return train_loader, test_loader, synth_loader


def build_model(hps, log):
    model = SmartVocoder(hps)
    n_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    # print(model)
    print('number of parameters:', n_params)
    state = {}
    state['n_params'] = n_params
    log.write('%s\n' % json.dumps(state))
    log.flush()

    return model


def train(epoch, model, optimizer, scaler, scheduler, log_train, args):
    global global_step
    global start_time

    epoch_loss = 0.0
    running_loss = [0., 0., 0.]
    log_interval = args.log_interval
    synth_interval = args.synth_interval

    timestemp = time.time()
    model.train()
    criterion_frame = nn.MSELoss()

    for batch_idx, (x, c) in enumerate(train_loader):
        global_step += 1
        optimizer.zero_grad()
        with autocast():
            x, c = x.to(device), c.to(device)
            log_p, log_det = model(x, c)
            loss = -(log_p + log_det)

        scaler.scale(loss).backward()

        # with autocast():
        #     z = torch.randn_like(x)
        #     y_gen = model.reverse(z, c)     

        # stft_est = stft(y_gen[:, 0], scale='linear')
        # stft_gt = stft(x[:, 0], scale='linear')
        # loss_frame = 0.2 * criterion_frame(stft_est, stft_gt)
        # scaler.scale(loss_frame).backward()
        
        # if torch.isnan(loss) or torch.isnan(loss_frame):
        #     continue

        scaler.step(optimizer)
        scaler.update()
        scheduler.step()

        running_loss[0] += loss.item()
        running_loss[1] += log_p.item()
        running_loss[2] += log_det.item()

        epoch_loss += loss.item()

        if (batch_idx + 1) % log_interval == 0:
            epoch_step = batch_idx + 1
            running_loss[0] /= log_interval
            running_loss[1] /= log_interval
            running_loss[2] /= log_interval
            avg_rn_loss = np.array(running_loss)
            avg_time = (time.time() - timestemp) / log_interval

            print('Global Step : {}, [{}, {}] [NLL, Log p(z), Log Det] : {},  avg time: {:0.4f}'
                  .format(global_step, epoch, epoch_step, avg_rn_loss,  avg_time))

            state = {}
            state['Global Step'] = global_step
            state['Epoch'] = epoch
            state['Epoch Step'] = epoch_step
            state['NLL, Log p(z), Log Det'] = running_loss
            state['avg time'] = avg_time
            state['total time'] = time.time() - start_time
            log_train.write('%s\n' % json.dumps(state))
            log_train.flush()

            timestemp = time.time()
            running_loss = [0., 0., 0.]

        if (batch_idx + 1) % synth_interval == 0:
            with torch.no_grad():
                synthesize(model, args.num_sample, args.sr)
            model.train()
            
        del x, c, log_p, log_det, loss

    del running_loss
    gc.collect()

    print('{} Epoch Training Loss : {:.4f}'.format(epoch, epoch_loss / (len(train_loader))))

    return epoch_loss / len(train_loader)


def evaluate(epoch, model, log_eval):
    global global_step
    global start_time

    running_loss = [0., 0., 0.]
    epoch_loss = 0.
    timestemp = time.time()

    model.eval()
    for _, (x, c) in enumerate(test_loader):
        with autocast():
            x, c = x.to(device), c.to(device)
            log_p, log_det = model(x, c)
            loss = -(log_p + log_det)
            if torch.isnan(loss):
                continue

        running_loss[0] += loss.item()
        running_loss[1] += log_p.item()
        running_loss[2] += log_det.item()
        epoch_loss += loss.item()

        del x, c, log_p, log_det, loss

    running_loss[0] /= len(test_loader)
    running_loss[1] /= len(test_loader)
    running_loss[2] /= len(test_loader)
    avg_rn_loss = np.array(running_loss)
    avg_time = (time.time() - timestemp) / len(test_loader)
    print('Global Step : {}, [{}, Eval] [NLL, Log p(z), Log Det] : {}, avg time: {:0.4f}'
          .format(global_step, epoch, avg_rn_loss, avg_time))

    state = {}
    state['Global Step'] = global_step
    state['Epoch'] = epoch
    state['NLL, Log p(z), Log Det'] = running_loss
    state['avg time'] = avg_time
    state['total time'] = time.time() - start_time
    log_eval.write('%s\n' % json.dumps(state))
    log_eval.flush()

    del running_loss

    epoch_loss /= len(test_loader)
    print('Evaluation Loss : {:.4f}'.format(epoch_loss))

    return epoch_loss


def synthesize(model, num_sample, sr):
    global global_step

    model.eval()
    for batch_idx, (x, c) in enumerate(synth_loader):
        if batch_idx < num_sample:
            x, c = x.to(device), c.to(device)
            q_0 = Normal(x.new_zeros(x.size()), x.new_ones(x.size()))
            z = q_0.sample()
            timestemp = time.time()
            with torch.no_grad():
                y_gen = model.reverse(z, c).squeeze()

            wav = y_gen.to(torch.device("cpu")).data.numpy()
            wav_name = '{}/generate_{}_{}.wav'.format(
                sample_path, global_step, batch_idx)
            print('{} seconds'.format(time.time() - timestemp))
            librosa.output.write_wav(wav_name, wav, sr=sr)
            print('{} Saved!'.format(wav_name))

            wav_orig = x.squeeze().to(torch.device("cpu")).data.numpy()
            wav_orig_name = '{}/orig_{}.wav'.format(
                sample_path, batch_idx)
            librosa.output.write_wav(wav_orig_name, wav_orig, sr=sr)

            del x, c, z, q_0, y_gen, wav
        
        else:
            break


def save_checkpoint(model, optimizer, scaler, scheduler, global_step, global_epoch):
    checkpoint_path = os.path.join(
        save_path, "checkpoint_step{:09d}.pth".format(global_step))
    optimizer_state = optimizer.state_dict()
    scaler_state = scaler.state_dict()
    scheduler_state = scheduler.state_dict()
    torch.save({"state_dict": model.state_dict(),
                "optimizer": optimizer_state,
                "scheduler": scheduler_state,
                "scaler_state": scaler_state,
                "global_step": global_step,
                "global_epoch": global_epoch}, checkpoint_path)


def load_checkpoint(step, model, optimizer, scheduler):
    checkpoint_path = os.path.join(
        load_path, "checkpoint_step{:09d}.pth".format(step))
    print("Load checkpoint from: {}".format(checkpoint_path))
    checkpoint = torch.load(checkpoint_path)

    # generalized load procedure for both single-gpu and DataParallel models
    # https://discuss.pytorch.org/t/solved-keyerror-unexpected-key-module-encoder-embedding-weight-in-state-dict/1686/3
    try:
        model.load_state_dict(checkpoint["state_dict"])
    except RuntimeError:
        print("INFO: this model is trained with DataParallel. Creating new state_dict without module...")
        state_dict = checkpoint["state_dict"]
        from collections import OrderedDict
        new_state_dict = OrderedDict()
        for k, v in state_dict.items():
            name = k[7:]  # remove `module.`
            new_state_dict[name] = v
        model.load_state_dict(new_state_dict)

    optimizer.load_state_dict(checkpoint["optimizer"])
    scheduler.load_state_dict(checkpoint["scheduler"])
    g_epoch = checkpoint["global_epoch"]
    g_step = checkpoint["global_step"]

    return model, optimizer, scheduler, g_epoch, g_step


if __name__ == "__main__":
    global global_step
    global start_time

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    args = parse_args()
    sample_path, save_path, load_path, log_path = mkdir(args)
    log, log_train, log_eval = get_logger(log_path, args.model_name)
    train_loader, test_loader, synth_loader = load_dataset(args)
    hps = Hyperparameters(args)
    model = build_model(hps, log)
    model.to(device)

    optimizer = optim.Adam(model.parameters(), lr=args.lr)
    scaler = GradScaler()
    scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=args.step_size, gamma=args.gamma)

    state = {k: v for k, v in args._get_kwargs()}

    if args.load_step == 0:
        # new model
        global_epoch = 0
        global_step = 0
        actnorm_init(train_loader, model, device)

    else:
        # saved model
        model, optimizer, scheduler, global_epoch, global_step = load_checkpoint(args.load_step, model, optimizer, scheduler)
        log.write('\n ! --- load the model and continue training --- ! \n')
        log_train.write('\n ! --- load the model and continue training --- ! \n')
        log_eval.write('\n ! --- load the model and continue training --- ! \n')
        log.flush()
        log_train.flush()
        log_eval.flush()

    start_time = time.time()
    dateTime = datetime.datetime.fromtimestamp(start_time).strftime('%Y-%m-%d %H:%M:%S')
    print('training starts at ', dateTime)

    for epoch in range(global_epoch + 1, args.epochs + 1):
        training_epoch_loss = train(epoch, model, optimizer, scaler, scheduler, log_train, args)

        with torch.no_grad():
            eval_epoch_loss = evaluate(epoch, model, log_eval)

        state['training_loss'] = training_epoch_loss
        state['eval_loss'] = eval_epoch_loss
        state['epoch'] = epoch
        log.write('%s\n' % json.dumps(state))
        log.flush()
        save_checkpoint(model, optimizer, scaler, scheduler, global_step, epoch)
        print('Epoch {} Model Saved! Loss : {:.4f}'.format(epoch, eval_epoch_loss))
        with torch.no_grad():
            synthesize(model, args.num_sample, args.sr)
        gc.collect()

    log_train.close()
    log_eval.close()
    log.close()
