#!/usr/bin/env python
"""
 Copyright 2018 Johns Hopkins University  (Author: Jesus Villalba)
 Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)
"""
import logging
import multiprocessing
import os
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from jsonargparse import (
    ActionConfigFile,
    ActionParser,
    ArgumentParser,
    namespace_to_dict,
)

from hyperion.hyp_defs import config_logger, set_float_cpu
from hyperion.torch import TorchModelLoader as TML
from hyperion.torch.adv_attacks import AttackFactory
from hyperion.torch.data import AudioDataset as AD
from hyperion.torch.data import ClassWeightedSeqSampler as Sampler
from hyperion.torch.data import SegSamplerFactory
from hyperion.torch.metrics import CategoricalAccuracy
from hyperion.torch.models import EfficientNetXVector as EXVec
from hyperion.torch.models import ResNet1dXVector as R1dXVec
from hyperion.torch.models import ResNetXVector as RXVec
from hyperion.torch.models import SpineNetXVector as SpineXVec
from hyperion.torch.models import TDNNXVector as TDXVec
from hyperion.torch.models import TransformerXVectorV1 as TFXVec
from hyperion.torch.narchs import AudioFeatsMVN as AF
from hyperion.torch.trainers import XVectorAdvTrainerFromWav as Trainer
from hyperion.torch.utils import ddp

xvec_dict = {
    "resnet": RXVec,
    "resnet1d": R1dXVec,
    "efficientnet": EXVec,
    "tdnn": TDXVec,
    "transformer": TFXVec,
    "spinenet": SpineXVec,
}


def init_data(partition, rank, num_gpus, **kwargs):
    kwargs = kwargs["data"][partition]
    ad_args = AD.filter_args(**kwargs["dataset"])
    sampler_args = kwargs["sampler"]
    if rank == 0:
        logging.info("{} audio dataset args={}".format(partition, ad_args))
        logging.info("{} sampler args={}".format(partition, sampler_args))
        logging.info("init %s dataset", partition)

    is_val = partition == "val"
    ad_args["is_val"] = is_val
    sampler_args["shuffle"] = not is_val
    dataset = AD(**ad_args)

    if rank == 0:
        logging.info("init %s samplers", partition)

    sampler = SegSamplerFactory.create(dataset, **sampler_args)

    if rank == 0:
        logging.info("init %s dataloader", partition)

    num_workers = kwargs["data_loader"]["num_workers"]
    num_workers_per_gpu = int((num_workers + num_gpus - 1) / num_gpus)
    largs = (
        {"num_workers": num_workers_per_gpu, "pin_memory": True} if num_gpus > 0 else {}
    )
    data_loader = torch.utils.data.DataLoader(dataset, batch_sampler=sampler, **largs)
    return data_loader


def init_feats(rank, **kwargs):
    feat_args = AF.filter_args(**kwargs["feats"])
    if rank == 0:
        logging.info("feat args={}".format(feat_args))
        logging.info("initializing feature extractor")
    feat_extractor = AF(trans=True, **feat_args)
    if rank == 0:
        logging.info("feat-extractor={}".format(feat_extractor))
    return feat_extractor


def init_xvector(num_classes, in_model_file, rank, xvec_class, **kwargs):
    xvec_args = xvec_class.filter_finetune_args(**kwargs["model"])
    if rank == 0:
        logging.info("xvector network ft args={}".format(xvec_args))
    xvec_args["num_classes"] = num_classes
    model = TML.load(in_model_file)
    model.change_config(**xvec_args)
    if rank == 0:
        logging.info("x-vector-model={}".format(model))
    return model


def init_hard_prototype_mining(model, train_loader, val_loader, rank):
    try:
        hard_prototype_mining = train_loader.batch_sampler.hard_prototype_mining
    except:
        hard_prototype_mining = False

    if not hard_prototype_mining:
        return

    if rank == 0:
        logging.info("setting hard prototypes")

    affinity_matrix = model.compute_prototype_affinity()
    train_loader.batch_sampler.set_hard_prototypes(affinity_matrix)

    try:
        hard_prototype_mining = val_loader.batch_sampler.hard_prototype_mining
    except:
        hard_prototype_mining = False

    if not hard_prototype_mining:
        return

    val_loader.batch_sampler.set_hard_prototypes(affinity_matrix)


def init_attack(feat_extractor, model, wav_scale, **kwargs):
    victim_model = nn.Sequential(feat_extractor, model)
    attack_args = AttackFactory.filter_args(**kwargs["attack"])
    extra_args = {
        "eps_scale": wav_scale,
        "loss": nn.functional.cross_entropy,
        "time_dim": 1,
    }
    attack_args.update(extra_args)
    logging.info("attacks args={}".format(attack_args))
    attack = AttackFactory.create(victim_model, **attack_args)
    return attack


def train_xvec(gpu_id, args):
    config_logger(args.verbose)
    del args.verbose
    logging.debug(args)

    kwargs = namespace_to_dict(args)
    torch.manual_seed(args.seed)
    set_float_cpu("float32")

    ddp_args = ddp.filter_ddp_args(**kwargs)
    device, rank, world_size = ddp.ddp_init(gpu_id, **ddp_args)
    kwargs["rank"] = rank

    train_loader = init_data(partition="train", **kwargs)
    val_loader = init_data(partition="val", **kwargs)
    feat_extractor = init_feats(**kwargs)
    model = init_xvector(list(train_loader.dataset.num_classes.values())[0], **kwargs)
    init_hard_prototype_mining(model, train_loader, val_loader, rank)
    kwargs["wav_scale"] = train_loader.dataset.wav_scale
    attack = init_attack(feat_extractor, model, **kwargs)

    trn_args = Trainer.filter_args(**kwargs["trainer"])
    if rank == 0:
        logging.info("trainer args={}".format(trn_args))
    metrics = {"acc": CategoricalAccuracy()}
    trainer = Trainer(
        model,
        feat_extractor,
        attack,
        device=device,
        metrics=metrics,
        ddp=world_size > 1,
        **trn_args
    )
    trainer.load_last_checkpoint()
    trainer.fit(train_loader, val_loader)

    ddp.ddp_cleanup()


def make_parser(xvec_class):
    parser = ArgumentParser()

    parser.add_argument("--cfg", action=ActionConfigFile)

    train_parser = ArgumentParser(prog="")

    AD.add_class_args(train_parser, prefix="dataset", skip={})
    SegSamplerFactory.add_class_args(train_parser, prefix="sampler")
    train_parser.add_argument(
        "--data_loader.num-workers",
        type=int,
        default=5,
        help="num_workers of data loader",
    )

    val_parser = ArgumentParser(prog="")
    AD.add_class_args(val_parser, prefix="dataset", skip={})
    SegSamplerFactory.add_class_args(val_parser, prefix="sampler")
    val_parser.add_argument(
        "--data_loader.num-workers",
        type=int,
        default=5,
        help="num_workers of data loader",
    )
    data_parser = ArgumentParser(prog="")
    data_parser.add_argument("--train", action=ActionParser(parser=train_parser))
    data_parser.add_argument("--val", action=ActionParser(parser=val_parser))
    parser.add_argument("--data", action=ActionParser(parser=data_parser))
    parser.link_arguments(
        "data.train.dataset.class_files", "data.val.dataset.class_files"
    )
    parser.link_arguments(
        "data.train.data_loader.num_workers", "data.val.data_loader.num_workers"
    )

    AF.add_class_args(parser, prefix="feats")
    xvec_class.add_finetune_args(parser, prefix="model")
    AttackFactory.add_class_args(parser, prefix="attack")

    parser.add_argument("--in-model-file", required=True)
    Trainer.add_class_args(
        parser, prefix="trainer", train_modes=xvec_class.valid_train_modes()
    )
    ddp.add_ddp_args(parser)
    parser.add_argument("--seed", type=int, default=1123581321, help="random seed")
    parser.add_argument(
        "-v", "--verbose", dest="verbose", default=1, choices=[0, 1, 2, 3], type=int
    )

    return parser


def main():
    parser = ArgumentParser(
        description="""Fine-tune x-vector model from audio files 
        with adversarial training"""
    )
    parser.add_argument("--cfg", action=ActionConfigFile)

    subcommands = parser.add_subcommands()
    for k, v in xvec_dict.items():
        parser_k = make_parser(v)
        subcommands.add_subcommand(k, parser_k)

    args = parser.parse_args()
    try:
        gpu_id = int(os.environ["LOCAL_RANK"])
    except:
        gpu_id = 0

    xvec_type = args.subcommand
    args_sc = vars(args)[xvec_type]

    if gpu_id == 0:
        try:
            config_file = Path(args_sc.trainer.exp_path) / "config.yaml"
            parser.save(args, str(config_file), format="yaml", overwrite=True)
        except:
            pass

    args_sc.xvec_class = xvec_dict[xvec_type]
    # torch docs recommend using forkserver
    multiprocessing.set_start_method("forkserver")
    train_xvec(gpu_id, args_sc)


if __name__ == "__main__":
    main()


# def init_data(
#     audio_path,
#     train_list,
#     val_list,
#     train_aug_cfg,
#     val_aug_cfg,
#     num_workers,
#     num_gpus,
#     rank,
#     **kwargs
# ):

#     ad_args = AD.filter_args(**kwargs)
#     sampler_args = Sampler.filter_args(**kwargs)
#     if rank == 0:
#         logging.info("audio dataset args={}".format(ad_args))
#         logging.info("sampler args={}".format(sampler_args))
#         logging.info("init datasets")

#     train_data = AD(audio_path, train_list, aug_cfg=train_aug_cfg, **ad_args)
#     val_data = AD(audio_path, val_list, aug_cfg=val_aug_cfg, is_val=True, **ad_args)

#     if rank == 0:
#         logging.info("init samplers")
#     train_sampler = Sampler(train_data, **sampler_args)
#     val_sampler = Sampler(val_data, **sampler_args)

#     num_workers_per_gpu = int((num_workers + num_gpus - 1) / num_gpus)
#     largs = (
#         {"num_workers": num_workers_per_gpu, "pin_memory": True} if num_gpus > 0 else {}
#     )

#     train_loader = torch.utils.data.DataLoader(
#         train_data, batch_sampler=train_sampler, **largs
#     )

#     test_loader = torch.utils.data.DataLoader(
#         val_data, batch_sampler=val_sampler, **largs
#     )

#     return train_loader, test_loader


# def init_feats(rank, **kwargs):
#     feat_args = AF.filter_args(**kwargs["feats"])
#     if rank == 0:
#         logging.info("feat args={}".format(feat_args))
#         logging.info("initializing feature extractor")
#     feat_extractor = AF(trans=True, **feat_args)
#     if rank == 0:
#         logging.info("feat-extractor={}".format(feat_extractor))
#     return feat_extractor


# def init_xvector(num_classes, in_model_path, rank, train_mode, **kwargs):
#     xvec_args = XVec.filter_finetune_args(**kwargs)
#     if rank == 0:
#         logging.info("xvector network ft args={}".format(xvec_args))
#     xvec_args["num_classes"] = num_classes
#     model = TML.load(in_model_path)
#     model.rebuild_output_layer(**xvec_args)
#     if train_mode == "ft-embed-affine":
#         model.freeze_preembed_layers()
#     if rank == 0:
#         logging.info("x-vector-model={}".format(model))
#     return model


# def init_attack(feat_extractor, model, wav_scale, **kwargs):
#     victim_model = nn.Sequential(feat_extractor, model)
#     attack_args = AttackFactory.filter_args(**kwargs["attack"])
#     extra_args = {
#         "eps_scale": wav_scale,
#         "loss": nn.functional.cross_entropy,
#         "time_dim": 1,
#     }
#     attack_args.update(extra_args)
#     logging.info("attacks args={}".format(attack_args))
#     attack = AttackFactory.create(victim_model, **attack_args)
#     return attack


# def train_xvec(gpu_id, args):

#     config_logger(args.verbose)
#     del args.verbose
#     logging.debug(args)

#     kwargs = namespace_to_dict(args)
#     torch.manual_seed(args.seed)
#     set_float_cpu("float32")

#     train_mode = kwargs["train_mode"]

#     ddp_args = ddp.filter_ddp_args(**kwargs)
#     device, rank, world_size = ddp.ddp_init(gpu_id, **ddp_args)
#     kwargs["rank"] = rank

#     train_loader, test_loader = init_data(**kwargs)
#     feat_extractor = init_feats(**kwargs)
#     model = init_xvector(train_loader.dataset.num_classes, **kwargs)
#     kwargs["wav_scale"] = train_loader.dataset.wav_scale
#     attack = init_attack(feat_extractor, model, **kwargs)

#     trn_args = Trainer.filter_args(**kwargs)
#     if rank == 0:
#         logging.info("trainer args={}".format(trn_args))
#     metrics = {"acc": CategoricalAccuracy()}
#     trainer = Trainer(
#         model,
#         feat_extractor,
#         attack,
#         device=device,
#         metrics=metrics,
#         ddp=world_size > 1,
#         train_mode=train_mode,
#         **trn_args
#     )
#     if args.resume:
#         trainer.load_last_checkpoint()
#     trainer.fit(train_loader, test_loader)

#     ddp.ddp_cleanup()


# if __name__ == "__main__":

#     parser = ArgumentParser(
#         description="Fine-tune x-vector model with adv attacks on wav domain"
#     )

#     parser.add_argument("--cfg", action=ActionConfigFile)
#     parser.add_argument("--audio-path", required=True)
#     parser.add_argument("--train-list", dest="train_list", required=True)
#     parser.add_argument("--val-list", dest="val_list", required=True)

#     AD.add_argparse_args(parser)
#     Sampler.add_argparse_args(parser)

#     parser.add_argument("--train-aug-cfg", default=None)
#     parser.add_argument("--val-aug-cfg", default=None)

#     parser.add_argument(
#         "--num-workers", type=int, default=5, help="num_workers of data loader"
#     )

#     AF.add_class_args(parser, prefix="feats")
#     parser.add_argument("--in-model-path", required=True)

#     XVec.add_finetune_args(parser)
#     AttackFactory.add_class_args(parser, prefix="attack")

#     Trainer.add_class_args(parser)
#     ddp.add_ddp_args(parser)

#     # parser.add_argument('--num-gpus', type=int, default=1,
#     #                     help='number of gpus, if 0 it uses cpu')
#     parser.add_argument(
#         "--seed", type=int, default=1123581321, help="random seed (default: 1)"
#     )
#     parser.add_argument(
#         "--resume",
#         action="store_true",
#         default=False,
#         help="resume training from checkpoint",
#     )
#     parser.add_argument(
#         "--train-mode",
#         default="ft-full",
#         choices=["ft-full", "ft-embed-affine"],
#         help=(
#             "ft-full: adapt full x-vector network"
#             "ft-embed-affine: adapt affine transform before embedding"
#         ),
#     )

#     # parser.add_argument('--attack-eps', required=True, type=float,
#     #                    help='epsilon adversarial attack')
#     # parser.add_argument('--attack-eps-step', required=True, type=float,
#     #                    help='eps step adversarial attack')
#     # parser.add_argument('--attack-random-eps', default=False,
#     #                    action='store_true',
#     #                    help='use random eps in adv. attack')

#     # parser.add_argument('--attack-max-iter', default=10, type=int,
#     #                    help='number of iterations for adversarial optimization')

#     # parser.add_argument('--p-attack', default=0.5, type=float,
#     #                    help='ratio of batches with adv attack')

#     parser.add_argument(
#         "-v", "--verbose", dest="verbose", default=1, choices=[0, 1, 2, 3], type=int
#     )
#     parser.add_argument("--local_rank", default=0, type=int)

#     args = parser.parse_args()
#     gpu_id = args.local_rank
#     del args.local_rank

#     if gpu_id == 0:
#         try:
#             config_file = Path(args.exp_path) / "config.yaml"
#             parser.save(args, str(config_file), format="yaml", overwrite=True)
#         except:
#             pass

#     # torch docs recommend using forkserver
#     multiprocessing.set_start_method("forkserver")
#     train_xvec(gpu_id, args)