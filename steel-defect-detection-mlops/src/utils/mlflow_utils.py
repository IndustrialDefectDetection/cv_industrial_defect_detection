import mlflow
import os
from typing import Dict, Any

class MLflowTracker:
    def __init__(self, experiment_name: str, model_name: str):
        self.experiment_name = experiment_name
        self.model_name = model_name
        mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "./mlruns"))
        mlflow.set_experiment(self.experiment_name)
        self.run = None

    def start_run(self, run_name: str = None, tags: Dict[str, str] = None):
        self.run = mlflow.start_run(run_name=run_name, tags=tags)
        return self.run.info.run_id

    def log_training_params(self, params: Dict[str, Any]):
        mlflow.log_params(params)

    def log_metrics(self, metrics: Dict[str, float]):
        mlflow.log_metrics(metrics)

    def log_artifact(self, path: str):
        mlflow.log_artifact(path)

    def end_run(self):
        mlflow.end_run()

def log_yolov8_training(tracker: MLflowTracker, training_args: Dict[str, Any], results_dir: str, model_path: str):
    # Log training parameters
    tracker.log_training_params(training_args)
    # Log model artifact
    if os.path.exists(model_path):
        tracker.log_artifact(model_path)
    # Log additional artifacts (plots, confusion matrix, etc.)
    if os.path.isdir(results_dir):
        for fname in os.listdir(results_dir):
            fpath = os.path.join(results_dir, fname)
            if os.path.isfile(fpath):
                tracker.log_artifact(fpath)
