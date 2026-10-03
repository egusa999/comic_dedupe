"""負けた巻の退避(重複フォルダへの移動 / 削除)。

既定は「移動」。削除は delete=True(CLI の --delete-losers)を明示したときだけ行う。
dry_run=True のときは一切ファイルを触らず、予定だけ Action として返す。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Sequence

from . import constants as C
from .logging_setup import get_logger
from .models import Action, Decision, VolumeItem


def unique_destination(path: Path) -> Path:
    """既に同名が存在する場合に `name (2).zip` 形式で退避先を作る。"""

    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for counter in range(2, C.MAX_RENAME_ATTEMPTS + 2):
        candidate = path.with_name(f"{stem} ({counter}){suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"退避先の名前を決められない: {path}")


def dispose_losers(
    decisions: Sequence[Decision],
    work_root: Path,
    dup_dir_name: str = C.DUP_DIR_NAME,
    delete: bool = False,
    dry_run: bool = False,
) -> list[Action]:
    """各グループの敗者を重複フォルダへ移動(または削除)する。"""

    logger = get_logger()
    actions: list[Action] = []
    for decision in decisions:
        for loser in decision.losers:
            reason = decision.reasons.get(loser.label(), "")
            if delete:
                actions.append(_delete_item(loser, reason, dry_run, logger))
            else:
                actions.append(
                    _move_item(loser, work_root, dup_dir_name, reason, dry_run, logger)
                )
    return actions


def _delete_item(item: VolumeItem, reason: str, dry_run: bool, logger) -> Action:
    action = Action(
        item_label=item.label(),
        kind="delete",
        source=str(item.path),
        detail=reason,
        result=C.RESULT_PLANNED,
    )
    if dry_run:
        logger.info("[予定] 削除: %s", item.label())
        return action
    try:
        if item.kind == "imageset":
            for member in item.member_files:
                member.unlink()
        elif item.path.is_dir():
            shutil.rmtree(item.path)
        else:
            item.path.unlink()
        action.result = C.RESULT_DELETED
        logger.info("削除: %s", item.label())
    except OSError as exc:
        action.result = C.RESULT_SKIPPED
        action.detail = f"削除に失敗: {exc}"
        logger.exception("削除に失敗: %s", item.label())
    return action


def _move_item(
    item: VolumeItem,
    work_root: Path,
    dup_dir_name: str,
    reason: str,
    dry_run: bool,
    logger,
) -> Action:
    destination = _destination_for(item, work_root, dup_dir_name)
    action = Action(
        item_label=item.label(),
        kind="move",
        source=str(item.path),
        destination=str(destination),
        detail=reason,
        result=C.RESULT_PLANNED,
    )
    if dry_run:
        logger.info("[予定] 退避: %s → %s", item.label(), destination.name)
        return action
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if item.kind == "imageset":
            destination.mkdir(parents=True, exist_ok=True)
            for member in item.member_files:
                shutil.move(str(member), str(destination / member.name))
            item.member_files = tuple(
                destination / member.name for member in item.member_files
            )
            item.path = destination
            item.kind = "folder"
        else:
            final = unique_destination(destination)
            shutil.move(str(item.path), str(final))
            item.path = final
            action.destination = str(final)
        action.result = C.RESULT_MOVED
        logger.info("退避: %s → %s", item.label(), action.destination)
    except OSError as exc:
        action.result = C.RESULT_SKIPPED
        action.detail = f"退避に失敗: {exc}"
        logger.exception("退避に失敗: %s", item.label())
    return action


def _destination_for(item: VolumeItem, work_root: Path, dup_dir_name: str) -> Path:
    """重複フォルダ内の退避先パス(元の相対階層を保つ)。"""

    dup_root = work_root / dup_dir_name
    if item.kind == "imageset":
        parent_relative = _relative_parent(item.path, work_root)
        return dup_root / parent_relative / f"{item.path.name} 第{item.title.volume}巻"
    relative = _relative_parent(item.path, work_root)
    return dup_root / relative / item.path.name


def _relative_parent(path: Path, work_root: Path) -> Path:
    try:
        relative = path.parent.relative_to(work_root)
    except ValueError:
        return Path()
    return relative
