from __future__ import print_function

import argparse
import os

from tensorboardX import SummaryWriter
from torchvision import datasets, transforms, models
import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import DistributedSampler
import time


def train(args, model, device, train_loader, epoch, writer):
    model.train()
    optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum)

    total_samples = 0
    start_time = time.time()  # Start timing

    if args.use_syn:
        data = torch.randn(args.batch_size, 3, 224, 224).to(device)
        target = torch.randint(0, 1000, (args.batch_size,)).to(device)
        for batch_idx in range(args.num_syn_batches):
            optimizer.zero_grad()
            output = model(data)
            loss = F.cross_entropy(output, target)
            loss.backward()
            optimizer.step()

            total_samples += len(data)  # Update total samples processed

            if batch_idx % args.log_interval == 0:
                print(
                    "Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss={:.6f}".format(
                        epoch,
                        batch_idx * len(data),
                        len(train_loader.dataset),
                        100.0 * batch_idx / len(train_loader),
                        loss.item(),
                    )
                )
                niter = epoch * len(train_loader) + batch_idx
                writer.add_scalar("Loss", loss.item(), niter)            
    else:
        for batch_idx, (data, target) in enumerate(train_loader):
            # Attach tensors to the device.
            data, target = data.to(device), target.to(device)

            for r in range(args.repeat):
                optimizer.zero_grad()
                output = model(data)
                loss = F.cross_entropy(output, target)
                loss.backward()
                optimizer.step()

                total_samples += len(data)  # Update total samples processed

                if batch_idx % args.log_interval == 0:
                    print(
                        "Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss={:.6f}".format(
                            epoch,
                            batch_idx * len(data),
                            len(train_loader.dataset),
                            100.0 * batch_idx / len(train_loader),
                            loss.item(),
                        )
                    )
                    niter = epoch * len(train_loader) + batch_idx
                    writer.add_scalar("Loss", loss.item(), niter)

    elapsed_time = time.time() - start_time
    throughput = total_samples / elapsed_time
    if dist.get_rank() == 0:
        print(f"Epoch {epoch}: Throughput is {throughput:.2f} samples/sec")


def test(model, device, test_loader, writer, epoch):
    model.eval()

    correct = 0
    with torch.no_grad():
        for data, target in test_loader:
            # Attach tensors to the device.
            data, target = data.to(device), target.to(device)

            output = model(data)
            # Get the index of the max log-probability.
            pred = output.max(1, keepdim=True)[1]
            correct += pred.eq(target.view_as(pred)).sum().item()

    accuracy = float(correct) / len(test_loader.dataset)
    print("\nTest set: Accuracy: {}/{} ({:.0f}%)\n".format(correct, len(test_loader.dataset), 100. * accuracy))
    writer.add_scalar("Accuracy", accuracy, epoch)


def main():
    # Training settings
    parser = argparse.ArgumentParser(description="PyTorch CIFAR10 ResNet152 Training")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=128,
        metavar="N",
        help="input batch size for training (default: 128)",
    )
    parser.add_argument(
        "--test-batch-size",
        type=int,
        default=1000,
        metavar="N",
        help="input batch size for testing (default: 1000)",
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=10,
        metavar="N",
        help="number of epochs to train (default: 10)",
    )
    parser.add_argument(
        "--lr",
        type=float,
        default=0.1,
        metavar="LR",
        help="learning rate (default: 0.1)",
    )
    parser.add_argument(
        "--momentum",
        type=float,
        default=0.9,
        metavar="M",
        help="SGD momentum (default: 0.9)",
    )
    parser.add_argument(
        "--no-cuda",
        action="store_true",
        default=False,
        help="disables CUDA training",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=1,
        metavar="S",
        help="random seed (default: 1)",
    )
    parser.add_argument(
        "--log-interval",
        type=int,
        default=10,
        metavar="N",
        help="how many batches to wait before logging training status",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=10,
        metavar="R",
        help="how many times we repeat each batch (for inflating the dataset size)",
    )
    parser.add_argument(
        "--num-workers",
        type=int,
        default=10,
        help="number of workers for dataloader",
    )
    parser.add_argument(
        "--save-model",
        action="store_true",
        default=False,
        help="For Saving the current Model",
    )
    parser.add_argument(
        "--dir",
        default="logs",
        metavar="L",
        help="directory where summary logs are stored",
    )

    parser.add_argument(
        "--backend",
        type=str,
        help="Distributed backend",
        choices=[dist.Backend.GLOO, dist.Backend.NCCL, dist.Backend.MPI],
        default=dist.Backend.GLOO,
    )
    parser.add_argument(
        "--use-syn",
        action="store_true",
        default=False,
        help="Use synthetic data for training",
    )
    parser.add_argument(
        "--num-syn-batches",
        type=int,
        default=25,
        help="number steps for benchmark using synthetic data",
    )
    args = parser.parse_args()
    use_cuda = not args.no_cuda and torch.cuda.is_available()
    if use_cuda:
        print("Using CUDA")
        if args.backend != dist.Backend.NCCL:
            print(
                "Warning: `nccl` distributed backend is recommended for the best performance with GPUs."
            )

    writer = SummaryWriter(args.dir)

    torch.manual_seed(args.seed)

    device = torch.device("cuda" if use_cuda else "cpu")

    # Load the resnet152 model
    model = models.resnet152(pretrained=False).to(device)

    print("Using distributed PyTorch with {} backend".format(args.backend))
    # Set distributed training environment variables to run this training script locally.
    if "WORLD_SIZE" not in os.environ:
        os.environ["RANK"] = "0"
        os.environ["WORLD_SIZE"] = "1"
        os.environ["MASTER_ADDR"] = "localhost"
        os.environ["MASTER_PORT"] = "1234"

    print(f"World Size: {os.environ['WORLD_SIZE']}. Rank: {os.environ['RANK']}")

    dist.init_process_group(backend=args.backend)
    Distributor = nn.parallel.DistributedDataParallel
    model = Distributor(model)

    # Load CIFAR10 dataset
    transform_train = transforms.Compose([
        transforms.RandomHorizontalFlip(),
        transforms.RandomCrop(32, padding=4),
        transforms.ToTensor(),
        transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
    ])

    transform_test = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
    ])

    train_ds = datasets.CIFAR10(
        root='/opt/data',
        train=True,
        download=True,
        transform=transform_train
    )

    test_ds = datasets.CIFAR10(
        root='/opt/data',
        train=False,
        download=True,
        transform=transform_test
    )

    train_loader = torch.utils.data.DataLoader(
        train_ds,
        batch_size=args.batch_size,
        sampler=DistributedSampler(train_ds),
        num_workers=args.num_workers
    )

    test_loader = torch.utils.data.DataLoader(
        test_ds,
        batch_size=args.test_batch_size,
        sampler=DistributedSampler(test_ds),
    )

    for epoch in range(1, args.epochs + 1):
        train(args, model, device, train_loader, epoch, writer)
        test(model, device, test_loader, writer, epoch)

    if args.save_model:
        torch.save(model.state_dict(), "resnet152_cifar10.pt")


if __name__ == "__main__":
    main()