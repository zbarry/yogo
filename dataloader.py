import os
import csv
import yaml
import torch

from pathlib import Path

from torchvision import datasets
from torchvision.io import read_image
from torch.utils.data import DataLoader, random_split
from torchvision.transforms import (
    Compose,
    Resize,
    RandomHorizontalFlip,
    RandomVerticalFlip,
)

from typing import Any, List, Dict, Union, Tuple, Optional, Callable, cast


class ObjectDetectionDataset(datasets.VisionDataset):
    def __init__(
        self,
        classes: List[str],
        image_path: Path,
        label_path: Path,
        loader: Callable = read_image,
        extensions: Optional[Tuple[str]] = ("png",),
        is_valid_file: Optional[Callable[[str], bool]] = None,
        *args,
        **kwargs,
    ):
        # the super().__init__ just sets transforms
        # the image_path is just for repr essentially
        super().__init__(str(image_path), *args, **kwargs)

        self.classes = classes
        self.image_folder_path = image_path
        self.label_folder_path = label_path
        self.loader = loader

        self.samples = self.make_dataset(
            is_valid_file=is_valid_file, extensions=extensions
        )

    def make_dataset(
        self,
        extensions: Optional[Union[str, Tuple[str, ...]]] = None,
        is_valid_file: Optional[Callable[[str], bool]] = None,
    ) -> List[Tuple[str, List[List[float]]]]:
        """
        torchvision.datasets.folder.make_dataset doc string states:
            "Generates a list of samples of a form (path_to_sample, class)"

        This is designed for a dataset for classficiation (that is, mapping
        image to class), where we have a dataset for object detection (image
        to list of bounding boxes).

        Copied Pytorch's implementation of input handling[0], with changes on how we
        collect labels and images

        [0] https://pytorch.org/vision/stable/_modules/torchvision/datasets/folder.html
        """
        both_none = extensions is None and is_valid_file is None
        both_something = extensions is not None and is_valid_file is not None
        if both_none or both_something:
            raise ValueError(
                "Both extensions and is_valid_file cannot be None or not None at the same time"
            )

        if extensions is not None:

            def is_valid_file(x: str) -> bool:
                return datasets.folder.has_file_allowed_extension(x, extensions)

        is_valid_file = cast(Callable[[str], bool], is_valid_file)

        # maps file name to a list of tuples of bounding boxes + classes
        samples: List[Tuple[str, List[List[float]]]] = []
        for img_file_path in self.image_folder_path.glob("*"):
            if is_valid_file(str(img_file_path)):
                labels = self.load_labels_from_image_name(img_file_path)
                samples.append((str(img_file_path), labels))
        return samples

    def load_labels_from_image_name(self, image_path: Path) -> List[List[float]]:
        "loads labels from label file, given by image path"
        labels = []
        label_filename = image_path.name.replace(image_path.suffix, ".csv")

        with open(self.label_folder_path / label_filename, "r") as f:
            # yuck! checking for headers is not super easy
            reader = csv.reader(f)
            has_header = csv.Sniffer().has_header(f.read(1024))
            f.seek(0)
            if has_header:
                next(reader, None)

            for row in reader:
                assert (
                    len(row) == 5
                ), "should have [class,xc,yc,w,h] - got length {len(row)}"
                labels.append([float(v) for v in row])

        return labels

    def __getitem__(self, index: int) -> Tuple[Any, Any]:
        """From torchvision.datasets.folder.DatasetFolder

        Args:
            index (int): Index

        Returns:
            tuple: (sample, target) where target is class_index of the target class.
        """
        path, target = self.samples[index]
        sample = self.loader(path)
        if self.transform is not None:
            sample = self.transform(sample)
        if self.target_transform is not None:
            target = self.target_transform(target)

        return sample, target

    def __len__(self) -> int:
        "From torchvision.datasets.folder.DatasetFolder"
        return len(self.samples)


def load_dataset_description(
    dataset_description,
) -> Tuple[List[str], Path, Path, Dict[str, float]]:
    with open(dataset_description, "r") as desc:
        yaml_data = yaml.safe_load(desc)

        classes = yaml_data["class_names"]
        image_path = Path(yaml_data["image_path"])
        label_path = Path(yaml_data["label_path"])
        split_fractions = {
            k: float(v) for k, v in yaml_data["dataset_split_fractions"].items()
        }

        if not sum(split_fractions.values()) == 1:
            raise ValueError(
                f"invalid split fractions for dataset: split fractions must add to 1, got {split_fractions}"
            )

        if not (image_path.is_dir() and label_path.is_dir()):
            raise FileNotFoundError(
                f"image_path or label_path do not lead to a directory\n{image_path=}\n{label_path=}"
            )

        return classes, image_path, label_path, split_fractions


def get_datasets(
    dataset_description_file: str,
    batch_size: int,
    training: bool = True,
) -> Dict[str, Any]:
    # TODO: Figure out typehint above
    (
        classes,
        image_path,
        label_path,
        split_fractions,
    ) = load_dataset_description(dataset_description_file)

    # TODO: How do transforms map labels? until we fix,
    training = False
    augmentations = (
        [RandomHorizontalFlip(0.5), RandomVerticalFlip(0.5)] if training else []
    )
    transforms = Compose([Resize([150, 200]), *augmentations])

    full_dataset = ObjectDetectionDataset(
        classes,
        image_path,
        label_path,
        transform=transforms,
    )

    dataset_sizes = {
        designation: int(split_fractions[designation] * len(full_dataset))
        for designation in ["train", "val"]
    }
    test_dataset_size = {"test": len(full_dataset) - sum(dataset_sizes.values())}
    split_sizes = {**dataset_sizes, **test_dataset_size}

    assert all([sz > 0 for sz in split_sizes.values()]) and sum(
        split_sizes.values()
    ) == len(
        full_dataset
    ), f"could not create valid dataset split sizes: {split_sizes}, full dataset size is {len(full_dataset)}"

    # YUCK! Want a map from the dataset designation to teh set itself, but "random_split" takes a list
    # of lengths of dataset. So we do this verbose rigamarol.
    return dict(
        zip(
            ["train", "val", "test"],
            random_split(
                full_dataset,
                [split_sizes["train"], split_sizes["val"], split_sizes["test"]],
            ),
        )
    )


def get_dataloader(
    root_dir: str,
    batch_size: int,
    split_percentages: List[float] = [1],
    training: bool = True,
):
    split_datasets = get_datasets(root_dir, batch_size, training=training)
    return {
        designation: DataLoader(
            dataset, batch_size=batch_size, shuffle=True, drop_last=True
        )
        for designation, dataset in split_datasets.items()
    }


if __name__ == "__main__":
    ODL = get_datasets("healthy_cell_dataset.yml", batch_size=128)
    print({k: len(d) for k, d in ODL.items()})
