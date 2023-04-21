import asyncio
import os
import re
import posixpath
from typing import *
from urllib.parse import urlparse

import torch
from fairseq import checkpoint_utils
from pydub import AudioSegment

from .cmd_opts import opts
from .inference.models import SynthesizerTrnMs256NSFSid, SynthesizerTrnMs256NSFSidNono
from .inference.pipeline import VC
from .shared import ROOT_DIR, device, is_half
from .utils import donwload_file, load_audio

AUDIO_OUT_DIR = opts.output_dir or os.path.join(ROOT_DIR, "outputs")


def update_state_dict(state_dict):
    if "params" in state_dict and state_dict["params"] is not None:
        return
    keys = [
        "spec_channels",
        "segment_size",
        "inter_channels",
        "hidden_channels",
        "filter_channels",
        "n_heads",
        "n_layers",
        "kernel_size",
        "p_dropout",
        "resblock",
        "resblock_kernel_sizes",
        "resblock_dilation_sizes",
        "upsample_rates",
        "upsample_initial_channel",
        "upsample_kernel_sizes",
        "spk_embed_dim",
        "gin_channels",
        "emb_channels",
        "sr",
    ]
    state_dict["params"] = {}
    n = 0
    for i, key in enumerate(keys):
        i = i - n
        if len(state_dict["config"]) != 19 and key == "emb_channels":
            # backward compat.
            state_dict["params"][key] = 256
            n += 1
            continue
        state_dict["params"][key] = state_dict["config"][i]


class VC_MODEL:
    def __init__(self, model_name: str, state_dict: Dict[str, Any]) -> None:
        update_state_dict(state_dict)
        self.model_name = model_name
        self.weight = state_dict
        self.tgt_sr = state_dict["params"]["sr"]
        f0 = state_dict.get("f0", 1)
        self.embedder_name = state_dict.get("embedder_name", "hubert_base")
        state_dict["params"]["spk_embed_dim"] = state_dict["weight"][
            "emb_g.weight"
        ].shape[0]
        if not "emb_channels" in state_dict["params"]:
            state_dict["params"]["emb_channels"] = 256  # for backward compat.

        if f0 == 1:
            self.net_g = SynthesizerTrnMs256NSFSid(
                **state_dict["params"], is_half=is_half
            )
        else:
            self.net_g = SynthesizerTrnMs256NSFSidNono(**state_dict["params"])

        del self.net_g.enc_q

        self.net_g.load_state_dict(state_dict["weight"], strict=False)
        self.net_g.eval().to(device)

        if is_half:
            self.net_g = self.net_g.half()
        else:
            self.net_g = self.net_g.float()

        self.vc = VC(self.tgt_sr, device, is_half)
        self.n_spk = state_dict["params"]["spk_embed_dim"]

    def single(
        self,
        sid: int,
        input_audio: str,
        f0_up_key: int,
        f0_file: str,
        f0_method: str,
        auto_load_index: bool,
        faiss_index_file: str,
        big_npy_file: str,
        index_rate: float,
    ):
        if not input_audio:
            raise Exception("You need to set Source Audio")
        f0_up_key = int(f0_up_key)
        audio = load_audio(input_audio, 16000)

        load_emb_dic = {
            "hubert_base": ("hubert_base.pt", "hubert_base"),
            "hubert_base768": ("hubert_base.pt", "hubert_base"),
            "contentvec": ("checkpoint_best_legacy_500.pt", "contentvec"),
            "contentvec768": ("checkpoint_best_legacy_500.pt", "contentvec"),
        }
        if not self.embedder_name in load_emb_dic.keys():
            raise Exception(f"Not supported embedder: {self.embedder_name}")
        if (
            embedder_model == None
            or loaded_embedder_model != load_emb_dic[self.embedder_name][1]
        ):
            print(f"load {self.embedder_name} embedder")
            load_embedder(
                load_emb_dic[self.embedder_name][0], load_emb_dic[self.embedder_name][1]
            )
        f0 = self.weight.get("f0", 1)

        if not faiss_index_file and auto_load_index:
            faiss_index_file = self.get_index_path(sid)
        if not big_npy_file and auto_load_index:
            big_npy_file = self.get_big_npy_path(sid)

        audio_opt = self.vc(
            embedder_model,
            self.net_g,
            sid,
            audio,
            f0_up_key,
            f0_method,
            faiss_index_file,
            big_npy_file,
            index_rate,
            f0,
            f0_file=f0_file,
            embedder_name=self.embedder_name,
        )

        audio = AudioSegment(
            audio_opt,
            frame_rate=self.tgt_sr,
            sample_width=2,
            channels=1,
        )
        os.makedirs(AUDIO_OUT_DIR, exist_ok=True)
        input_audio_splitext = os.path.splitext(os.path.basename(input_audio))[0]
        model_splitext = os.path.splitext(self.model_name)[0]
        index = 0
        existing_files = os.listdir(AUDIO_OUT_DIR)
        for existing_file in existing_files:
            result = re.match(r"\d+", existing_file)
            if result:
                prefix_num = int(result.group(0))
                if index < prefix_num:
                    index = prefix_num
        audio.export(
            os.path.join(
                AUDIO_OUT_DIR, f"{index+1}-{model_splitext}-{input_audio_splitext}.wav"
            ),
            format="wav",
        )
        return audio_opt

    def get_big_npy_path(self, speaker_id: int):
        basename = os.path.splitext(self.model_name)[0]
        speaker_big_npy_path = os.path.join(
            MODELS_DIR,
            "checkpoints",
            f"{basename}_index",
            f"{basename}.{speaker_id}.big.npy",
        )
        if os.path.exists(speaker_big_npy_path):
            return speaker_big_npy_path
        return os.path.join(MODELS_DIR, "checkpoints", f"{basename}.big.npy")

    def get_index_path(self, speaker_id: int):
        basename = os.path.splitext(self.model_name)[0]
        speaker_index_path = os.path.join(
            MODELS_DIR,
            "checkpoints",
            f"{basename}_index",
            f"{basename}.{speaker_id}.index",
        )
        if os.path.exists(speaker_index_path):
            return speaker_index_path
        return os.path.join(MODELS_DIR, "checkpoints", f"{basename}.index")


MODELS_DIR = opts.models_dir or os.path.join(ROOT_DIR, "models")
vc_model: Optional[VC_MODEL] = None
embedder_model = None
loaded_embedder_model = ""


def download_models():
    loop = asyncio.new_event_loop()
    tasks = []
    for template in [
        "D{}k",
        "G{}k",
        "f0D{}k",
        "f0G{}k",
    ]:
        for sr in ["32", "40", "48"]:
            url = f"https://huggingface.co/ddPn08/rvc_pretrained/resolve/main/{template.format(sr)}.pth"
            out = os.path.join(MODELS_DIR, "pretrained", f"{template.format(sr)}.pth")
            if os.path.exists(out):
                continue
            tasks.append(loop.run_in_executor(None, donwload_file, url, out))

    if len(tasks) > 0:
        loop.run_until_complete(asyncio.gather(*tasks))

    for url in [
        "https://huggingface.co/ddPn08/rvc_pretrained/resolve/main/hubert_base.pt",
        "https://huggingface.co/innnky/contentvec/resolve/main/checkpoint_best_legacy_500.pt",
    ]:
        out = os.path.join(MODELS_DIR, posixpath.basename(urlparse(url).path))
        if not os.path.exists(out):
            donwload_file(url, out)


def get_models():
    dir = os.path.join(ROOT_DIR, "models", "checkpoints")
    os.makedirs(dir, exist_ok=True)
    return [
        file
        for file in os.listdir(dir)
        if any([x for x in [".ckpt", ".pth"] if file.endswith(x)])
    ]


def load_embedder(emb_file, emb_name):
    global embedder_model, loaded_embedder_model
    models, _, _ = checkpoint_utils.load_model_ensemble_and_task(
        [os.path.join(MODELS_DIR, emb_file)],
        suffix="",
    )
    embedder_model = models[0]
    embedder_model = embedder_model.to(device)
    if is_half:
        embedder_model = embedder_model.half()
    else:
        embedder_model = embedder_model.float()
    embedder_model.eval()

    loaded_embedder_model = emb_name


def load_hubert():
    global embedder_model, loaded_embedder_model
    models, _, _ = checkpoint_utils.load_model_ensemble_and_task(
        [os.path.join(MODELS_DIR, "hubert_base.pt")],
        suffix="",
    )
    embedder_model = models[0]
    embedder_model = embedder_model.to(device)
    if is_half:
        embedder_model = embedder_model.half()
    else:
        embedder_model = embedder_model.float()
    embedder_model.eval()

    loaded_embedder_model = "hubert_base"


def load_contentvec():
    global embedder_model, loaded_embedder_model
    models, _, _ = checkpoint_utils.load_model_ensemble_and_task(
        [os.path.join(MODELS_DIR, "checkpoint_best_legacy_500.pt")],
        suffix="",
    )
    embedder_model = models[0]
    embedder_model = embedder_model.to(device)
    if is_half:
        embedder_model = embedder_model.half()
    else:
        embedder_model = embedder_model.float()
    embedder_model.eval()

    loaded_embedder_model = "contentvec"


def get_vc_model(model_name: str):
    model_path = os.path.join(MODELS_DIR, "checkpoints", model_name)
    weight = torch.load(model_path, map_location="cpu")
    return VC_MODEL(model_name, weight)


def load_model(model_name: str):
    global vc_model
    vc_model = get_vc_model(model_name)
