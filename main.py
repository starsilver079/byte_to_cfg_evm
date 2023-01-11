import os
import sys
import argparse

from typing import Optional, Union
from crytic_compile import cryticparser
from cfglib import CFG

result_dir = "generated_cfg"

def output_to_dot(d: str, filename: str, cfg: CFG) -> None:
    if not os.path.exists(d):
        os.makedirs(d)
    filename = os.path.basename(filename)
    filename = os.path.join(d, filename + "_")
    cfg.output_to_dot(filename)
    for function in cfg.functions:
        function.output_to_dot(filename)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="python __main__.py", usage="python __main__.py xxxx.xxx"
    )

    parser.add_argument("filename", help="bytecode.evm")

    cryticparser.init(parser)

    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)
    args = parser.parse_args()
    return args


def generate(bytecode: Optional[Union[str, bytes]], filename: str, args: argparse.Namespace) -> None:
    optimization_enabled = False
    cfg = CFG(
        bytecode, optimization_enabled=optimization_enabled, compute_cfgs=True
    )
    output_to_dot(result_dir, filename, cfg)


def main() -> None:

    args = parse_args()

    with open(args.filename, "rb") as f:
        bytecode = f.read()
    generate(bytecode, args.filename, args)

if __name__ == "__main__":
    main()
