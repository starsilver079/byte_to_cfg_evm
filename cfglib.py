import logging
import re
from typing import Optional, Union, Tuple, List, Dict

from pyevmasm import disassemble_all, Instruction

from block import BasicBlock
from action import Function


logger = logging.getLogger("evm-cfg-builder")

BASIC_BLOCK_END = [
    "STOP",
    "SELFDESTRUCT",
    "RETURN",
    "REVERT",
    "INVALID",
    "SUICIDE",
    "JUMP",
    "JUMPI",
]


def convert_bytecode(bytecode: Optional[Union[str, bytes]]) -> Optional[bytes]:
    if bytecode is not None:
        if isinstance(bytecode, str):
            for library_found in re.findall(r"__.{36}__", bytecode):
                logger.info("Replace library %s by %s", library_found, "A" * 40)
            bytecode = re.sub(r"__.{36}__", "A" * 40, bytecode)

            bytecode = bytecode.replace("\n", "")
            if bytecode.startswith("0x"):
                bytecode = bytes.fromhex(bytecode[2:])
            else:
                bytecode = bytes.fromhex(bytecode)
        else:
            for library_found in re.findall(b"__.{36}__", bytecode):
                logger.info("Replace library %s by %s", library_found, "A" * 40)
            bytecode = re.sub(b"__.{36}__", b"A" * 40, bytecode)
            if bytecode.startswith(b"0x"):
                bytecode = bytes.fromhex(bytecode[2:].decode().replace("\n", ""))

    return bytecode


class CFG:
    def __init__(
        self,
        bytecode: Optional[Union[str, bytes]] = None,
        remove_metadata: bool = True,
        analyze: bool = True,
        optimization_enabled: bool = True,
        compute_cfgs: bool = True,
    ) -> None:
        self._functions: Dict[int, Function] = {}
        self._basic_blocks: Dict[int, BasicBlock] = {}
        self._instructions: Dict[int, Instruction] = {}

        self._optimization_enabled = optimization_enabled

        assert isinstance(bytecode, (type(None), str, bytes))

        self._bytecode = convert_bytecode(bytecode)

        if remove_metadata:
            self.remove_metadata()
        if analyze:
            self.create_functions()
            if compute_cfgs:
                self.create_cfgs()

    def __repr__(self) -> str:
        return f"<CFG: {len(self.functions)} Functions, {len(self.basic_blocks)} Basic Blocks>"

    @property
    def bytecode(self) -> Optional[bytes]:
        return self._bytecode

    @bytecode.setter
    def bytecode(self, bytecode: Optional[Union[str, bytes]]) -> None:
        assert isinstance(bytecode, (type(None), str, bytes))

        bytecode = convert_bytecode(bytecode)

        self.clear()
        self._bytecode = bytecode

    @property
    def basic_blocks(self) -> List[BasicBlock]:
        bbs = self._basic_blocks.values()
        return list(set(bbs))

    @property
    def entry_point(self) -> BasicBlock:
        return self._basic_blocks[0]

    @property
    def functions(self) -> List[Function]:
        return list(self._functions.values())

    @property
    def instructions(self) -> List[Instruction]:
        return list(self._instructions.values())

    def get_instruction_at(self, addr: int) -> Instruction:
        return self._instructions.get(addr)

    def get_basic_block_at(self, addr: int) -> Optional[BasicBlock]:
        return self._basic_blocks.get(addr)

    def get_function_at(self, addr: int) -> Optional[Function]:
        return self._functions.get(addr)

    def create_functions(self) -> None:
        self.compute_basic_blocks()
        self.compute_functions(self._basic_blocks[0], True)
        self.add_function(Function(Function.DISPATCHER_ID, 0, self._basic_blocks[0], self))

    def create_cfgs(self) -> None:
        from evm_cfg_builder.value_analysis.value_set_analysis import StackValueAnalysis

        for function in self.functions:

            vsa = StackValueAnalysis(
                self, function.entry, function.hash_id, self._optimization_enabled
            )
            bbs = vsa.analyze()

            function.basic_blocks = [self._basic_blocks[bb] for bb in bbs]

            if function.hash_id != Function.DISPATCHER_ID:
                function.check_payable()
                function.check_view()
                function.check_pure()

    def clear(self) -> None:
        self._functions = {}
        self._basic_blocks = {}
        self._instructions = {}
        self._bytecode = bytes()

    def remove_metadata(self) -> None:
        if self.bytecode:
            self.bytecode = re.sub(
                bytes(
                    r"\xa1\x65\x62\x7a\x7a\x72\x30\x58\x20[\x00-\xff]{32}\x00\x29".encode("charmap")
                ),
                b"",
                self.bytecode,
            )

    def compute_basic_blocks(self) -> None:
        if self._basic_blocks:
            return

        bb = BasicBlock()

        for instruction in disassemble_all(self.bytecode):
            self._instructions[instruction.pc] = instruction

            if instruction.name == "JUMPDEST":
                if bb.instructions:
                    self._basic_blocks[bb.end.pc] = bb

                bb = BasicBlock()

                self._basic_blocks[instruction.pc] = bb

            bb.add_instruction(instruction)

            if bb.start.pc == instruction.pc:
                self._basic_blocks[instruction.pc] = bb

            if bb.end.name in BASIC_BLOCK_END:
                self._basic_blocks[bb.end.pc] = bb
                bb = BasicBlock()

    def compute_functions(self, block: "BasicBlock", is_entry_block: bool = False) -> None:
        if is_entry_block:
            if block.ends_with_jumpi():
                ins = [i.name for i in block.instructions]
                if "CALLVALUE" in ins:
                    assert len(block.instructions) > 2
                    push = block.instructions[-2]
                    assert push.name.startswith("PUSH")
                    destination = push.operand
                    true_branch = self._basic_blocks[destination]
                    self.compute_functions(true_branch)
                    return

        function_start, function_hash = is_jump_to_function(block)
        if function_start:
            if "GT" in [i.name for i in block.instructions]:
                next_branch = self._basic_blocks[function_start]
                self.compute_functions(next_branch)

            else:
                assert function_hash
                new_function = Function(
                    function_hash, function_start, self._basic_blocks[function_start], self
                )

                self._functions[function_start] = new_function

            if block.ends_with_jumpi():
                false_branch = self._basic_blocks[block.end.pc + 1]
                self.compute_functions(false_branch)

    def add_function(self, func: Function) -> None:
        assert isinstance(func, Function)
        self._functions[func.start_addr] = func

    def compute_simple_edges(self, key: int) -> None:
        for bb in self._basic_blocks.values():

            if bb.end.name == "JUMPI":
                dst = self._basic_blocks[bb.end.pc + 1]
                bb.add_outgoing_basic_block(dst, key)
                dst.add_incoming_basic_block(bb, key)

            if bb.end.name not in BASIC_BLOCK_END:
                try:
                    dst = self._basic_blocks[bb.end.pc + 1 + bb.end.operand_size]
                except KeyError:
                    continue
                assert dst.start.name == "JUMPDEST"
                bb.add_outgoing_basic_block(dst, key)
                dst.add_incoming_basic_block(bb, key)

    def compute_reachability(self, entry_point: "BasicBlock", key: int) -> None:
        bbs_saw = [entry_point]

        bbs_to_explore = [entry_point]
        while bbs_to_explore:
            bb = bbs_to_explore.pop()
            for son in bb.outgoing_basic_blocks(key):
                if not son in bbs_saw:
                    bbs_saw.append(son)
                    bbs_to_explore.append(son)

        for bb in bbs_saw:
            bb.reacheable.append(key)

        for bb in self._basic_blocks.values():
            if not bb in bbs_saw:
                if key in bb.incoming_basic_blocks_as_dict.keys():
                    bb.incoming_basic_blocks_as_dict.pop(key)
                if key in bb.outgoing_basic_blocks_as_dict.keys():
                    bb.outgoing_basic_blocks_as_dict.pop(key)

    def output_to_dot(self, base_filename: str) -> None:

        with open(f"{base_filename}-FULL_GRAPH.dot", "w", encoding="utf-8") as f:
            f.write("digraph{\n")
            for basic_block in self.basic_blocks:
                instructions_ = [f"{hex(ins.pc)}:{str(ins)}" for ins in basic_block.instructions]
                instructions = "\n".join(instructions_)

                f.write(f'{basic_block.start.pc}[shape=box,label="{instructions}"]\n')

                for son in basic_block.all_outgoing_basic_blocks:
                    f.write(f"{basic_block.start.pc} -> {son.start.pc}\n")

            f.write("\n}")


def is_jump_to_function(block: BasicBlock) -> Tuple[Optional[int], Optional[int]]:
    has_calldata_size = False
    last_pushed_value: Optional[int] = None
    previous_last_pushed_value: Optional[int] = None
    for i in block.instructions:
        if i.name == "CALLDATASIZE":
            has_calldata_size = True

        if i.name.startswith("PUSH"):
            previous_last_pushed_value = last_pushed_value
            last_pushed_value = i.operand

    if block.ends_with_jumpi() and has_calldata_size:
        return last_pushed_value, -1

    if block.ends_with_jumpi() and previous_last_pushed_value:
        return last_pushed_value, previous_last_pushed_value

    return None, None
