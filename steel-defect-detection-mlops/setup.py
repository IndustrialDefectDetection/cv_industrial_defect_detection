"""
Steel Defect Detection MLOps Pipeline
A comprehensive machine learning operations pipeline for steel surface defect detection using YOLOv8.
"""

from setuptools import setup, find_packages
import os

# Read README file
def read_readme():
    with open("README.md", "r", encoding="utf-8") as fh:
        return fh.read()

# Read requirements
def read_requirements():
    with open("requirements.txt", "r", encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip() and not line.startswith("#")]

setup(
    name="steel-defect-detection-mlops",
    version="1.0.0",
    author="Steel Defect Detection Team",
    author_email="your.email@example.com",
    description="MLOps pipeline for steel surface defect detection using YOLOv8",
    long_description=read_readme(),
    long_description_content_type="text/markdown",
    url="https://github.com/ylmzelff/steel-defect-detection-mlops",
    project_urls={
        "Bug Tracker": "https://github.com/ylmzelff/steel-defect-detection-mlops/issues",
        "Documentation": "https://github.com/ylmzelff/steel-defect-detection-mlops/wiki",
        "Source Code": "https://github.com/ylmzelff/steel-defect-detection-mlops",
    },
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: Scientific/Engineering :: Image Processing",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    python_requires=">=3.8",
    install_requires=read_requirements(),
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "black>=22.0.0",
            "flake8>=5.0.0",
            "mypy>=0.991",
            "pre-commit>=2.20.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "steel-defect-convert=data_preprocessing.xml_to_yolo:main",
            "steel-defect-split=data_preprocessing.split_data:main", 
            "steel-defect-train=training.train:main",
        ],
    },
    include_package_data=True,
    package_data={
        "": ["*.yaml", "*.yml", "*.json", "*.txt", "*.md"],
    },
    keywords=[
        "machine learning",
        "computer vision", 
        "object detection",
        "YOLOv8",
        "steel defect detection",
        "industrial AI",
        "quality control",
        "defect detection",
        "PyTorch",
    ],
    zip_safe=False,
)