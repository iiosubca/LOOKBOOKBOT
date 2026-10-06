"""Bounded, cached exact-photograph search before INDD initialization.

Catalogue outfit matching is deliberately NOT used here: the person, pose,
hands and actual requested shot must match, not merely clothing.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from .providers import CodexProvider, ProviderError, ProviderLimitError
from .vision_checkpoints import VisionCheckpoint, atomic_json


PHOTO_POLICY = """LOOKBOOKBOT_EXACT_PHOTO_RECOVERY_V1
Identify the EXACT requested photographs in the PDF LEFT and PDF RIGHT slots.
The same outfit on another person or in another pose is NOT the requested shot.
Compare person/face, head direction, hands, limbs, bag position and garment folds.
Retouch, brightness, resolution and cropping can differ; garment colour and
studio background alone are not identity evidence. Never use filenames, label
numbers or sort order as evidence. A missing shot must stay NONE, never replace
it with a different take. Do not use the credit-catalogue outfit exception.
"""


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def request(provider, root, look, prompt, images, labels=(), verification=False):
    checkpoint = VisionCheckpoint(root, provider, prompt, images, [look])
    cached = checkpoint.read()
    for attempt in range(2):
        raw = cached if cached and attempt == 0 else (
            provider.run_readonly_photo_pair_vision(prompt, root, images=images, look_id=look,
                                                   allowed_labels=labels, verification=verification)
            if isinstance(provider, CodexProvider) else provider.run_readonly_vision(prompt, root, images=images, timeout=600))
        try:
            data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
            if data.get("look_id") != look or len(str(data.get("note", "")).strip()) < 25:
                raise ValueError("Invalid identity observation")
            if verification:
                if not isinstance(data.get("accepted"), bool):
                    raise ValueError("Missing exact-photo decision")
            else:
                for side in ("left", "right"):
                    if data.get(side) not in {*labels, "NONE"}:
                        raise ValueError("Selection outside the supplied photo board")
                if data["left"] == data["right"] != "NONE":
                    raise ValueError("One photograph cannot fill both PDF slots")
            checkpoint.save(raw)
            return data
        except (ValueError, TypeError, AttributeError) as error:
            if attempt:
                raise ProviderError(f"{look}: непригодный ответ поиска точных фотографий: {error}") from error
            cached = None
    raise AssertionError("unreachable")


def recover_photos(provider, root: Path, controller, log, exclude_looks=(), reserved_filenames=()):
    folder = root / "control/work/photo-recovery"
    queue_path = folder / "manifest.json"
    if not queue_path.is_file():
        return []
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    if (root / "control/lookbook-state.json").exists():
        log("Фотографии уже инициализированного INDD автоматически не пересопоставляются; готовый файл сохранён.")
        return [target["look_id"] for target in queue["targets"]]
    accepted, unresolved = [], []
    reserved_hashes = {digest(root / "_MAT/hires" / name) for name in reserved_filenames
                       if (root / "_MAT/hires" / name).is_file()}
    interruption = None
    for target in queue["targets"]:
        look = target["look_id"]
        if look in exclude_looks:
            continue
        found = {"left": "NONE", "right": "NONE"}
        reference = root / target["reference_pair"]
        if digest(reference) != target["reference_pair_sha256"]:
            raise ProviderError(f"{look}: изменена пара PDF для поиска фотографий.")
        boards = target["boards"]
        try:
            for offset in range(0, len(boards), 2):
                batch = boards[offset:offset + 2]
                labels = [label for board in batch for label in board["labels"]
                          if queue["candidates"][label]["filename"] not in reserved_filenames
                          and queue["candidates"][label]["sha256"] not in reserved_hashes]
                if not labels:
                    continue
                images = [reference] + [root / board["path"] for board in batch]
                if any(digest(root / board["path"]) != board["sha256"] for board in batch):
                    raise ProviderError(f"{look}: изменились поисковые карточки фотографий.")
                prompt = PHOTO_POLICY + f"\nLOOK: {look}. First image: PDF LEFT/RIGHT. Remaining images: labelled unused hi-res candidates.\n"
                prompt += f"Choose only labels visible on THESE boards: {', '.join(labels)}, or NONE for each side. "
                prompt += f'Return JSON only: {{"look_id":"{look}","left":"NONE","right":"NONE","note":"at least two exact-shot cues"}}. '
                prompt += "If only one requested shot is present, select it independently; never discard it because the other is absent."
                for side in ("left", "right"):
                    if target.get(f"current_{side}"):
                        prompt += f" {side.upper()} already has its verified source; return NONE for this slot, choose only missing slots."
                answer = request(provider, root, look, prompt, images, labels)
                for side in found:
                    if not target.get(f"current_{side}") and found[side] == "NONE" and answer[side] != "NONE":
                        found[side] = answer[side]
                log(f"{look}: проверены страницы фото {offset + 1}–{min(offset + 2, len(boards))}/{len(boards)}.")
                if all(found[side] != "NONE" or target.get(f"current_{side}") for side in found):
                    break
            if not any(value != "NONE" for value in found.values()):
                unresolved.append(look)
                continue
            selection = folder / look / "selection.json"
            atomic_json(selection, {"decisions": [{"look_id": look, **found}]})
            controller.script("apply_photo_recovery.py", root, "--decisions", selection, "--render", timeout=180)
            proof = folder / look / "exact-proof.jpg"
            prompt = PHOTO_POLICY + f"\nVerify this exact proof for {look}: PDF LEFT, PDF RIGHT, proposed hi-res LEFT, proposed hi-res RIGHT. "
            prompt += f"Selected slots: LEFT={found['left']}, RIGHT={found['right']}. Verify EVERY non-white proposed hi-res slot, including already confirmed sources from a prior partial recovery; a white slot remains unresolved and is not a failed selected photo. "
            prompt += f'Return JSON only: {{"look_id":"{look}","accepted":true,"note":"two concrete exact-shot identity cues"}}. Reject a different shot or swapped sides.'
            checked = request(provider, root, look, prompt, [proof], verification=True)
            if checked["accepted"]:
                accepted.append({"look_id": look, **found, "accepted": True, "note": checked["note"],
                                 "proof": proof.relative_to(root).as_posix(), "proof_sha256": digest(proof)})
                if any(value == "NONE" for value in found.values()):
                    unresolved.append(look)
            else:
                unresolved.append(look)
        except ProviderLimitError as error:
            interruption = error
            break
        except (ProviderError, ValueError, OSError) as error:
            log(f"{look}: поиск фото не завершён, неподтверждённые кадры не подставляю: {error}")
            unresolved.append(look)
    if accepted:
        # One-to-one conflicts do not destroy independent positive results.
        claimed = {}
        for item in accepted:
            for side in ("left", "right"):
                if item[side] != "NONE":
                    claimed.setdefault(item[side], set()).add(item["look_id"])
        conflicts = {look for owners in claimed.values() if len(owners) > 1 for look in owners}
        ready = [item for item in accepted if item["look_id"] not in conflicts]
        unresolved.extend(sorted(conflicts))
        if ready:
            decisions = folder / "verified-decisions.json"
            atomic_json(decisions, {"decisions": ready})
            controller.script("apply_photo_recovery.py", root, "--decisions", decisions, timeout=180)
            log("Дополнительно найдены и проверены фотографии: " + ", ".join(item["look_id"] for item in ready))
    if interruption:
        raise interruption
    with (root / "control/work/look-register.tsv").open(encoding="utf-8-sig", newline="") as source:
        current = list(csv.DictReader(source, delimiter="\t"))
    return [row["look_id"] for row in current if any(row[key].startswith("__lbb_missing_") for key in ("left_filename", "right_filename"))]
