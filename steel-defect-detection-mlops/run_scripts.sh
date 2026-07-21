#!/bin/bash

python src/training/train.py --epochs 1 --no-mlflow 2>&1

# python src/training/train.py --epochs 10 --no-mlflow 2>&1

# python src/training/train.py --epochs 50 --no-mlflow 2>&1
