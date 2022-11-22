from collections import defaultdict
import os
import numpy as np
import torch
import torch.nn as nn
from tqdm import tqdm

from torch.utils.tensorboard import SummaryWriter

class emoTrainer:
    def __init__(self, 
                args, 
                generator,
                emotion_proc,
                disc_word_len,
                train_loader,
                val_loader):        
        self.args = args
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.generator = generator
        self.disc_word_len = disc_word_len
        self.emotion_proc = emotion_proc
        
        run_name = 'GAN_class' + str(self.args.emo_dim) + '_lrg' + str(args.lr_g) + '_lrd' + str(args.lr_dsc) + '_b0.6' + '_BCE_ReconsGANLoss_AddNoise_std0.5_lowpower_PretrainDisc400_Pad_CFCN'
        # run_name = '0'
        self.plotter = SummaryWriter('runs/' + run_name) 
        
        self.mse_loss = torch.nn.MSELoss(reduction='mean')
        self.emo_loss = nn.CrossEntropyLoss(reduction='mean')
        self.global_step = 0
    
    def freezeNet(self, network):
        for p in network.parameters():
            p.requires_grad = False
    
    def unfreezeNet(self, network):
        for p in network.parameters():
            p.requires_grad = True

    def schdulerStep(self):
        self.generator.module.scheduler.step()
        self.disc_word_len.module.scheduler.step()

    def displayLRs(self):
        lr_list = [self.generator.module.opt.param_groups]
        if self.args.disc_word_len:
            lr_list.append(self.disc_word_len.module.opt.param_groups)
        cnt = 0
        for lr in lr_list:
            for param_group in lr:
                print('LR {}: {}'.format(cnt, param_group['lr']))
                cnt+=1

    def sign_loss(self, relative_word_length, gen_relative_word_length):
        cosine = torch.cosine_similarity(relative_word_length, gen_relative_word_length, dim=1)
        return torch.mean(1 - cosine)

    def saveNetworks(self, fold):
        torch.save(self.generator.state_dict(), os.path.join(self.args.out_path, fold, 'generator.pt'))
        if self.args.disc_word_len:
            torch.save(self.disc_word_len.state_dict(), os.path.join(self.args.out_path, fold, 'disc_video.pt'))
        if self.args.disc_emo:
            torch.save(self.disc_emo.state_dict(), os.path.join(self.args.out_path, fold, 'disc_emo.pt'))
        print('Networks has been saved to {}'.format(fold))

    # BCE loss
    def GAN_BCELoss(self, logit, label):
        if label == 'real':
            return torch.mean(-torch.log(logit))
        if label == 'fake':
            return torch.mean(-torch.log(1-logit))

    def GAN_WasserteinLoss(self, logit, label):
        if label == 'real':
            return -logit.mean()
        if label == 'fake':
            return logit.mean()

    def step_disc_wordlen(self, data, epoch):
        self.disc_word_len.train()
        gen_relative_word_length, relative_word_length, gen_emo, emo_label = data
        self.disc_word_len.module.opt.zero_grad()
        
        # emo_proc = self.emotion_proc(emo_label)
        logit_fake = self.disc_word_len(emo_label, gen_relative_word_length)
        logit_real = self.disc_word_len(emo_label, relative_word_length)
        loss_fake = self.GAN_BCELoss(logit_fake, 'fake')
        loss_real = self.GAN_BCELoss(logit_real, 'real')
        loss = loss_fake + loss_real
        loss.backward()

        torch.nn.utils.clip_grad_norm_(self.disc_word_len.parameters(), 1.)
        self.disc_word_len.module.opt.step()

        # wdistance = -(loss_fake + loss_real).item()
        # self.loss_dict['df_wdistance'].append(wdistance)

        losslst = np.array([loss_fake.item(), loss_real.item(), loss.item()])
        return losslst
  
  
    def step_generator(self, data):
        self.disc_word_len.eval()

        self.generator.train()
        relative_word_length, emo_label, pos_vec = data
        self.generator.module.opt.zero_grad()
        gen_emotion, gen_relative_word_length  = self.generator(emo_label, pos_vec)
        
        df = self.disc_word_len.forward(emo_label, gen_relative_word_length)
        gan_loss = self.GAN_BCELoss(df, 'real')

        recon_loss = self.mse_loss(relative_word_length, gen_relative_word_length)
        sign_loss = self.sign_loss(relative_word_length, gen_relative_word_length)
        emo_loss = self.emo_loss(gen_emotion, torch.argmax(emo_label, dim=1))
        # print(sign_loss, emo_loss, recon_loss)
        
        loss =  gan_loss + 5*recon_loss #+ 0.5*sign_loss # + 0.1*emo_loss
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.generator.parameters(), 1.)
        self.generator.module.opt.step()

        if np.random.random() > 0.995:
            print(np.round(gen_relative_word_length[0,...].tolist(), 4), np.round(relative_word_length[0,...].tolist(), 4))

        losslst = np.array([recon_loss.item(),  emo_loss.item(), sign_loss.item(), gan_loss.item(), loss.item()])
        return losslst


    def train(self):
        for epoch in tqdm(range(self.args.num_epochs)):

            gen_losses = np.array([0.,0.,0.,0.,0.])
            dsc_losses = np.array([0.,0.,0.])
            diterator = iter(self.train_loader)
            for t in range(len(self.train_loader)):               
                relative_word_length, emotion, pos_vec = [d.float().to(self.args.device) for d in next(diterator)]
                data = [relative_word_length, emotion, pos_vec]
                                
                if self.global_step%2 == 0:
                    gen_losses += self.step_generator(data)
                else:
                    with torch.no_grad():
                        gen_emotion, gen_relative_word_length = self.generator(emotion, pos_vec)
                    data = [gen_relative_word_length, relative_word_length, gen_emotion, emotion]
                    dsc_losses += self.step_disc_wordlen(data, epoch)
                self.global_step += 1

            self.schdulerStep()

            length = len(self.train_loader)
            self.plotter.add_scalar("lossgen/recons", gen_losses[0]/length, epoch)
            self.plotter.add_scalar("lossgen/emo", gen_losses[1]/length, epoch)
            self.plotter.add_scalar("lossgen/sign", gen_losses[2]/length, epoch)
            self.plotter.add_scalar("lossgen/real", gen_losses[2]/length, epoch)
            self.plotter.add_scalar("lossgen/", gen_losses[3]/length, epoch)
            # self.plotter.add_scalar("learning_rate/", gen_losses[4], epoch)

            self.plotter.add_scalar("lossdisc/real", dsc_losses[0]/length, epoch)
            self.plotter.add_scalar("lossdisc/fake", dsc_losses[1]/length, epoch)
            self.plotter.add_scalar("lossdisc/", dsc_losses[2]/length, epoch)
        
        self.displayLRs()
        self.saveNetworks('')
        self.plotter.flush()
        self.plotter.close()


            


