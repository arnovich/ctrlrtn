"""Argument registration for dataset lineage manifests."""

from __future__ import annotations


def register_dataset(sub, commands) -> None:
    dataset = sub.add_parser(
        "dataset", help="create or verify inert offline dataset lineage"
    )
    dataset_sub = dataset.add_subparsers(dest="dataset_command", required=True)
    dataset_create = dataset_sub.add_parser(
        "create", help="freeze task-clustered train/evaluation trace membership"
    )
    dataset_create.add_argument("use_case")
    dataset_create.add_argument("output")
    dataset_create.add_argument("--limit", type=int, default=1000)
    dataset_create.add_argument("--train-percent", type=int, default=80)
    dataset_create.add_argument("--salt", default="default")
    dataset_create.add_argument("--workflow", default=None)
    dataset_create.add_argument("--workflow-version", default=None)
    dataset_create.add_argument("--step", default=None)
    dataset_create.set_defaults(func=commands._dataset_create)
    dataset_verify = dataset_sub.add_parser(
        "verify",
        help="verify manifest integrity and live trace payload bindings",
    )
    dataset_verify.add_argument("path")
    dataset_verify.set_defaults(func=commands._dataset_verify)
