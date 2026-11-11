"""Single WAV adapter for the previously frozen MERT v2 refinement artifacts."""

import argparse
import hashlib
import importlib.util
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from .errors import ValidationError
from .validation import GENRES, MOODS, decode_json

SELECTION_SHA256 = "69f0a3ce854b3ee567a238c60ba8791c96f0a377fe6dc737ea5e3f3bd434d208"
ENCODER_SHA256 = "e1643052104f24d4da39178b2253dfc8f7136a4999bd5541d3ec953f823d3c4f"


def model_inputs(baseline, refinement):
    selection_bytes = (refinement / "selection-frozen.json").read_bytes()
    if hashlib.sha256(selection_bytes).hexdigest() != SELECTION_SHA256:
        raise ValidationError("MODEL_SELECTION_CHANGED")
    selected = decode_json(selection_bytes)["selected"]
    plan = decode_json((baseline / "prepared/plan-frozen.json").read_bytes())
    spec = plan["encoders"]["mert2"]
    if (selected["config"]["id"] != "ensemble-mlp50" or selected["config"]["mlp_weight"] != 0.5
            or selected["policy"]["automatic_use_enabled"] is not False
            or plan["tags"] != {"genre": list(GENRES), "theme": list(MOODS)}
            or spec["weights_sha256"] != ENCODER_SHA256 or spec["layer_index"] != 23
            or spec["native_rate"] != 24000 or spec["dimension"] != 1024):
        raise ValidationError("MODEL_CONTRACT_MISMATCH")
    needed = [baseline / "models/mert2/model.safetensors", refinement / selected["inference"]["path"],
              refinement / "code/run.py", refinement / "code/refinement_core.py"]
    needed.extend(baseline / "code" / name for name in ("encoders.py", "features.py", "common.py", "classifiers.py"))
    if not all(path.is_file() for path in needed):
        raise ValidationError("MODEL_ARTIFACT_UNAVAILABLE")
    manifest = (refinement / selected["inference"]["path"]).read_bytes()
    if hashlib.sha256(manifest).hexdigest() != selected["inference"]["sha256"]:
        raise ValidationError("MODEL_SELECTION_CHANGED")
    members = decode_json(manifest)
    for member in (members["mlp"], members["svm"]):
        root = baseline if member["baseline"] else refinement
        if member["config"]["representation"] != "L24-mean" or not (root / member["inference"]["path"]).is_file():
            raise ValidationError("MODEL_ARTIFACT_UNAVAILABLE")
    dependencies = ("joblib", "numpy", "resource", "safetensors", "sklearn", "soundfile", "torch",
                    "torchaudio", "transformers")
    if any(importlib.util.find_spec(name) is None for name in dependencies):
        raise ValidationError("MODEL_ENVIRONMENT_UNAVAILABLE")
    return selected, spec


def predict(wav, baseline, refinement, selected, spec, device):
    # These two code directories belong to the locally controlled experiment.
    # Neither location, module name, nor model path comes from a server task.
    sys.path[:0] = [str(refinement / "code"), str(baseline / "code")]
    import numpy as np
    import soundfile as sf
    import classifiers
    import common
    import encoders
    import features
    import run as experiment

    experiment.BASE, experiment.WORK = baseline, refinement
    encoder = encoders.Encoder(baseline, "mert2", spec, device=device)
    try:
        try:
            audio_file = sf.SoundFile(wav)
        except (OSError, RuntimeError):
            raise ValidationError("INVALID_PREPARED_WAV") from None
        with audio_file as audio:
            if (audio.format not in {"WAV", "WAVEX", "RF64"} or audio.channels != 1
                    or audio.samplerate != 24000 or len(audio) < 24000):
                raise ValidationError("INVALID_PREPARED_WAV")
            ranges = common.boundaries(len(audio))
            vectors = []
            for left, right in ranges:
                try:
                    samples = audio.read(right - left, dtype="float32")
                except (OSError, RuntimeError):
                    raise ValidationError("INVALID_PREPARED_WAV") from None
                if len(samples) != right - left or not np.isfinite(samples).all():
                    raise ValidationError("INVALID_PREPARED_WAV")
                vectors.append(encoder.segment(samples))
        mean, deviation = features.summarize(vectors, ranges)
        _, scores = experiment.predict_selected(selected, {"L24": (mean[None, :], deviation[None, :])})
        if scores.shape != (1, len(GENRES) + len(MOODS)) or not np.isfinite(scores).all():
            raise ValidationError("INVALID_MODEL_OUTPUT")
        chosen = classifiers.representative(scores, selected["policy"]["representative_source_balanced_F0.5"])[0]
        return {"genre": [code for code, flag in zip(GENRES, chosen) if flag],
                "mood": [code for code, flag in zip(MOODS, chosen[len(GENRES):]) if flag]}
    finally:
        encoder.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="고정 MERT v2 앙상블로 WAV 한 곡의 후보를 추론합니다.")
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--refinement-root", type=Path, required=True)
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--worker-json-errors", action="store_true", help=argparse.SUPPRESS)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--check", action="store_true")
    inputs.add_argument("--wav", type=Path)
    args = parser.parse_args(argv)
    try:
        baseline, refinement = args.baseline_root.resolve(), args.refinement_root.resolve()
        selected, spec = model_inputs(baseline, refinement)
        with redirect_stdout(sys.stderr):
            output = {"artifacts_present": True} if args.check else predict(
                args.wav, baseline, refinement, selected, spec, args.device)
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    except ValidationError as exc:
        print(json.dumps({"error": exc.code}), file=sys.stdout if args.worker_json_errors else sys.stderr)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, ImportError):
        print('{"error":"MERT_INFERENCE_UNAVAILABLE"}', file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
