#! /usr/bin/env python3


import wandb
import torch

from torch.optim import AdamW
from torch.optim.lr_scheduler import LinearLR, SequentialLR, CosineAnnealingLR

from pathlib import Path
from copy import deepcopy
from typing_extensions import TypeAlias
from typing import Optional, Tuple, cast

from yogo.model import YOGO
from yogo.yogo_loss import YOGOLoss
from yogo.argparsers import train_parser
from yogo.utils import draw_rects, Metrics
from yogo.dataloader import (
    YOGO_CLASS_ORDERING,
    load_dataset_description,
    get_dataloader,
)
from yogo.cluster_anchors import best_anchor


# https://pytorch.org/tutorials/recipes/recipes/tuning_guide.html#enable-cudnn-auto-tuner
torch.backends.cudnn.benchmark = True
# https://pytorch.org/docs/stable/notes/cuda.html#tensorfloat-32-tf32-on-ampere-devices
torch.backends.cuda.matmul.allow_tf32 = True

# TODO find sync points
# https://pytorch.org/docs/stable/generated/torch.cuda.set_sync_debug_mode.html#torch-cuda-set-sync-debug-mode
# this will error out if a synchronizing operation occurs

# TUNING GUIDE - goes over this
# https://pytorch.org/tutorials/recipes/recipes/tuning_guide.html

# TODO
# measure forward / backward pass timing w/
# https://pytorch.org/docs/stable/notes/cuda.html#asynchronous-execution


def checkpoint_model(model, epoch, optimizer, name, step):
    torch.save(
        {
            "epoch": epoch,
            "step": step,
            "model_state_dict": deepcopy(model.state_dict()),
            "optimizer_state_dict": deepcopy(optimizer.state_dict()),
        },
        str(name),
    )


def train():
    config = wandb.config
    device = config["device"]
    anchor_w = config["anchor_w"]
    anchor_h = config["anchor_h"]
    class_names = config["class_names"]
    num_classes = len(class_names)
    classify = not config["no_classify"]

    if config.pretrained_path:
        net, global_step = YOGO.from_pth(config.pretrained_path)
        net.to(device)
        if any(net.img_size.cpu().numpy() != config["resize_shape"]):
            raise RuntimeError(
                "mismatch in pretrained network image resize shape and current resize shape: "
                f"pretrained network resize_shape = {net.img_size}, requested resize_shape = {config['resize_shape']}"
            )
    else:
        net = YOGO(
            num_classes=num_classes,
            img_size=config["resize_shape"],
            anchor_w=anchor_w,
            anchor_h=anchor_h,
        ).to(device)
        global_step = 0

    print('created network')

    Y_loss = YOGOLoss(classify=classify).to(device)
    optimizer = AdamW(net.parameters(), lr=config["learning_rate"])

    print('created loss and optimizer')

    metrics = Metrics(num_classes=num_classes, device=device, class_names=class_names)

    # TODO: generalize so we can tune Sx / Sy!
    # TODO: best way to make model architecture tunable?
    Sx, Sy = net.get_grid_size(config["resize_shape"])
    wandb.config.update({"Sx": Sx, "Sy": Sy})

    print('initializing dataset...')
    (
        model_save_dir,
        train_dataloader,
        validate_dataloader,
        test_dataloader,
    ) = init_dataset(config)
    print('dataset initialized...')

    min_period = 8 * len(train_dataloader)
    anneal_period = config["epochs"] * len(train_dataloader) - min_period
    lin = LinearLR(optimizer, start_factor=0.01, end_factor=1, total_iters=min_period)
    cs = CosineAnnealingLR(optimizer, T_max=anneal_period, eta_min=5e-5)
    scheduler = SequentialLR(optimizer, [lin, cs], [min_period])

    best_mAP = 0
    for epoch in range(config["epochs"]):
        # train
        for imgs, labels in train_dataloader:
            imgs = imgs.to(device, non_blocking=True)
            labels = labels.to(device, non_blocking=True)
            print(f'training loop step {global_step}')
            global_step += 1

            optimizer.zero_grad(set_to_none=True)

            outputs = net(imgs)
            loss = Y_loss(outputs, labels)
            loss.backward()

            optimizer.step()
            scheduler.step()

            wandb.log(
                {
                    "train loss": loss.item(),
                    "epoch": epoch,
                    "LR": scheduler.get_last_lr()[0],
                },
                commit=False,
                step=global_step,
            )

        wandb.log({"training grad norm": net.grad_norm()}, step=global_step)

        # do validation things
        val_loss = 0.0
        net.eval()
        with torch.no_grad():
            for imgs, labels in validate_dataloader:
                imgs = imgs.to(device, non_blocking=True)
                labels = labels.to(device, non_blocking=True)
                outputs = net(imgs)
                loss = Y_loss(outputs, labels)
                val_loss += loss.item()

            metrics.update(outputs, labels)

            annotated_img = wandb.Image(
                draw_rects(imgs[0, 0, ...].clone(), outputs[0, ...].clone(), thresh=0.1)
            )

            mAP, confusion_data = metrics.compute()
            metrics.reset()

            wandb.log(
                {
                    "validation bbs": annotated_img,
                    "val loss": val_loss / len(validate_dataloader),
                    "val mAP": mAP["map"],
                    "val confusion": get_wandb_confusion(
                        confusion_data, "validation confusion matrix"
                    ),
                },
            )

            if mAP["map"] > best_mAP:
                best_mAP = mAP["map"]
                wandb.log({"best_mAP_save": mAP["map"]}, step=global_step)
                checkpoint_model(
                    net, epoch, optimizer, model_save_dir / "best.pth", global_step,
                )
            else:
                checkpoint_model(
                    net, epoch, optimizer, model_save_dir / "latest.pth", global_step,
                )

        net.train()

    # do test things
    net.eval()
    test_loss = 0.0
    with torch.no_grad():
        for imgs, labels in test_dataloader:
            imgs = imgs.to(device, non_blocking=True)
            labels = labels.to(device, non_blocking=True)
            outputs = net(imgs)
            loss = Y_loss(outputs, labels)
            test_loss += loss.item()

        metrics.update(outputs, labels)

        mAP, confusion_data = metrics.compute()
        metrics.reset()
        wandb.log(
            {
                "test loss": test_loss / len(test_dataloader),
                "test mAP": mAP["map"],
                "test confusion": get_wandb_confusion(
                    confusion_data, "test confusion matrix"
                ),
            },
        )

        checkpoint_model(
            net,
            epoch,
            optimizer,
            model_save_dir / f"latest.pth",
            global_step,
        )


WandbConfig: TypeAlias = dict


def init_dataset(config: WandbConfig):
    dataloaders = get_dataloader(
        config["dataset_descriptor_file"],
        config["batch_size"],
        Sx=config["Sx"],
        Sy=config["Sy"],
        device=config["device"],
        preprocess_type=config["preprocess_type"],
        vertical_crop_size=config["vertical_crop_size"],
        resize_shape=config["resize_shape"],
    )

    train_dataloader = dataloaders["train"]
    validate_dataloader = dataloaders["val"]
    test_dataloader = dataloaders["test"]

    wandb.config.update(
        {  # we do this here b.c. batch_size can change wrt sweeps
            "training set size": f"{len(train_dataloader.dataset)} images",
            "validation set size": f"{len(validate_dataloader.dataset)} images",
            "testing set size": f"{len(test_dataloader.dataset)} images",
        }
    )

    if wandb.run.name is not None:
        model_save_dir = Path(f"trained_models/{wandb.run.name}")
    else:
        model_save_dir = Path(
            f"trained_models/unnamed_run_{torch.randint(100, size=(1,)).item()}"
        )
    model_save_dir.mkdir(exist_ok=True, parents=True)

    return model_save_dir, train_dataloader, validate_dataloader, test_dataloader


def get_wandb_confusion(confusion_data, title):
    return wandb.plot_table(
        "wandb/confusion_matrix/v1",
        wandb.Table(
            columns=["Actual", "Predicted", "nPredictions"], data=confusion_data,
        ),
        {"Actual": "Actual", "Predicted": "Predicted", "nPredictions": "nPredictions",},
        {"title": title},
    )


def do_training(args) -> None:
    device = torch.device(
        args.device
        if args.device is not None
        else ("cuda" if torch.cuda.is_available() else "cpu")
    )

    epochs = args.epochs or 64
    batch_size = args.batch_size or 32
    adam_lr = 3e-4

    preprocess_type: Optional[str]
    vertical_crop_size: Optional[float] = None

    if args.crop:
        vertical_crop_size = cast(float, args.crop)
        if not (0 < vertical_crop_size < 1):
            raise ValueError(
                "vertical_crop_size must be between 0 and 1; got {vertical_crop_size}"
            )
        resize_target_size = (round(vertical_crop_size * 772), 1032)
        preprocess_type = "crop"
    elif args.resize:
        resize_target_size = cast(Tuple[int, int], tuple(args.resize))
        preprocess_type = "resize"
    else:
        resize_target_size = (772, 1032)
        preprocess_type = None

    print('loading dataset description')
    class_names, dataset_paths, _ = load_dataset_description(
        args.dataset_descriptor_file
    )

    print('getting best anchor')
    anchor_w, anchor_h = best_anchor([d["label_path"] for d in dataset_paths])

    print('initting wandb')
    wandb.init(
        project="yogo",
        entity="bioengineering",
        config={
            "learning_rate": adam_lr,
            "epochs": epochs,
            "batch_size": batch_size,
            "device": str(device),
            "anchor_w": anchor_w,
            "anchor_h": anchor_h,
            "resize_shape": resize_target_size,
            "vertical_crop_size": vertical_crop_size,
            "preprocess_type": preprocess_type,
            "class_names": YOGO_CLASS_ORDERING,
            "pretrained_path": args.from_pretrained,
            "no_classify": args.no_classify,
            "run group": args.group,
            "dataset_descriptor_file": args.dataset_descriptor_file,
        },
        notes=args.note,
        tags=["v0.0.3"],
    )

    train()


if __name__ == "__main__":
    torch.multiprocessing.set_start_method("spawn")
    # torch.multiprocessing.set_sharing_strategy("file_system")

    # Potentially the end of my pain?
    # https://pytorch.org/docs/stable/data.html#multi-process-data-loading
    # https://github.com/pytorch/pytorch/issues/13246#issuecomment-905703662

    parser = train_parser()
    args = parser.parse_args()

    do_training(args)
