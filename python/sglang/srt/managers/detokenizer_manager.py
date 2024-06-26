"""DetokenizerManager is a process that detokenizes the token ids."""
import asyncio
import inspect

import uvloop
import zmq
import zmq.asyncio

from sglang.srt.hf_transformers_utils import get_tokenizer
from sglang.srt.managers.io_struct import BatchStrOut, BatchTokenIDOut
from sglang.srt.server_args import PortArgs, ServerArgs
from sglang.utils import get_exception_traceback, graceful_registry
from sglang.srt.managers.controller.infer_batch import FINISH_MATCHED_STR

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


class DetokenizerManager:
    def __init__(
        self,
        server_args: ServerArgs,
        port_args: PortArgs,
    ):
        context = zmq.asyncio.Context(2)
        self.recv_from_router = context.socket(zmq.PULL)
        self.recv_from_router.bind(f"tcp://127.0.0.1:{port_args.detokenizer_port}")

        self.send_to_tokenizer = context.socket(zmq.PUSH)
        self.send_to_tokenizer.connect(f"tcp://127.0.0.1:{port_args.tokenizer_port}")

        self.tokenizer = get_tokenizer(
            server_args.tokenizer_path,
            tokenizer_mode=server_args.tokenizer_mode,
            trust_remote_code=server_args.trust_remote_code,
        )

    async def handle_loop(self):
        while True:
            recv_obj: BatchTokenIDOut = await self.recv_from_router.recv_pyobj()
            assert isinstance(recv_obj, BatchTokenIDOut)

            output_tokens = recv_obj.output_tokens

            # TODO(lmzheng): handle skip_special_tokens/spaces_between_special_tokens per request
            output_strs = self.tokenizer.batch_decode(
                output_tokens,
                skip_special_tokens=recv_obj.skip_special_tokens[0],
                spaces_between_special_tokens=recv_obj.spaces_between_special_tokens[
                    0
                ],
            )

            # Trim stop str
            # TODO(lmzheng): handle the case where multiple stop strs are hit
            for i in range(len(output_strs)):
                if len(output_tokens[i]) > 0:
                    first_token = self.tokenizer.convert_ids_to_tokens(
                        int(output_tokens[i][0])
                    )
                    if not isinstance(first_token, str):
                        first_token = first_token.decode("utf-8", errors="ignore")
                    if first_token.startswith("▁"):
                        output_strs[i] = " " + output_strs[i]

                output_strs[i] = recv_obj.prev_output_strs[i] + output_strs[i]

                if isinstance(recv_obj.finished_reason[i], FINISH_MATCHED_STR):
                    pos = output_strs[i].find(recv_obj.finished_reason[i].matched)
                    if pos != -1:
                        output_strs[i] = output_strs[i][:pos]

            self.send_to_tokenizer.send_pyobj(
                BatchStrOut(
                    rids=recv_obj.rids,
                    output_str=output_strs,
                    meta_info=recv_obj.meta_info,
                    finished_reason=recv_obj.finished_reason,
                )
            )


def start_detokenizer_process(
    server_args: ServerArgs,
    port_args: PortArgs,
    pipe_writer,
):
    graceful_registry(inspect.currentframe().f_code.co_name)

    try:
        manager = DetokenizerManager(server_args, port_args)
    except Exception:
        pipe_writer.send(get_exception_traceback())
        raise
    pipe_writer.send("init ok")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(manager.handle_loop())
