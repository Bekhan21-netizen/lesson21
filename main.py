#!/usr/bin/env python3
"""
Эйзенхауэр To-Do Pro
=====================

Профессиональный консольный менеджер задач на основе Матрицы Эйзенхауэра.

Архитектура построена по принципу строгого разделения слоёв:

    Model   -> domain-сущности (Task, Quadrant, перечисления)
    Service -> бизнес-логика (TaskRepository, TaskManager, BackupService,
               HistoryService, ValidationService)
    UI      -> весь вывод и ввод (NotificationCenter, ConsoleUI)

Запуск:
    pip install rich
    python main.py

Требования: Python 3.12+
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import uuid
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import StrEnum, auto
from pathlib import Path
from typing import Any, Callable, Self

from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group
from rich.layout import Layout
from rich.padding import Padding
from rich.panel import Panel
from rich.pretty import Pretty
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.status import Status
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

# ======================================================================================
# КОНФИГУРАЦИЯ
# ======================================================================================


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Глобальная неизменяемая конфигурация приложения.

    Хранит все «магические» значения проекта в одном месте, чтобы
    избежать magic numbers/strings в бизнес-логике и UI.
    """

    app_name: str = "ЭЙЗЕНХАУЭР TO-DO PRO"
    app_tagline: str = (
        "Организуйте задачи по Матрице Эйзенхауэра и управляйте временем "
        "как профессионал."
    )
    app_version: str = "2.0.0"
    data_file: Path = Path("tasks.json")
    log_file: Path = Path("todo.log")
    backup_dir: Path = Path("backups")
    max_backups: int = 10
    history_limit: int = 50
    date_format: str = "%d.%m.%Y %H:%M"
    short_date_format: str = "%d.%m.%Y"
    json_indent: int = 2
    json_schema_version: int = 2
    table_box: box.Box = box.ROUNDED
    panel_box: box.Box = box.DOUBLE
    heavy_box: box.Box = box.HEAVY


class Theme:
    """Цветовая палитра интерфейса.

    Отдельный класс-неймспейс упрощает смену темы без правок UI-кода.
    """

    PRIMARY = "bold cyan"
    SECONDARY = "bold magenta"
    SUCCESS = "bold green"
    ERROR = "bold red"
    WARNING = "bold yellow"
    INFO = "bold blue"
    MUTED = "dim white"
    ACCENT = "bold white on dark_blue"

    URGENT_IMPORTANT = "bold white on red3"
    NOT_URGENT_IMPORTANT = "bold white on blue3"
    URGENT_NOT_IMPORTANT = "bold black on yellow3"
    NOT_URGENT_NOT_IMPORTANT = "bold black on green3"


CONFIG = AppConfig()

# ======================================================================================
# ЛОГИРОВАНИЕ
# ======================================================================================


def configure_logging(config: AppConfig) -> logging.Logger:
    """Настраивает логирование приложения в файл `todo.log`.

    Args:
        config: Конфигурация приложения с путём к лог-файлу.

    Returns:
        Настроенный объект логгера для всего приложения.
    """
    logger = logging.getLogger("eisenhower_todo")
    logger.setLevel(logging.DEBUG)

    handler = logging.FileHandler(config.log_file, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


LOGGER = configure_logging(CONFIG)


# ======================================================================================
# MODEL
# ======================================================================================


class Quadrant(StrEnum):
    """Квадранты Матрицы Эйзенхауэра.

    Каждый квадрант инкапсулирует своё отображаемое имя, цвет и иконку,
    что устраняет необходимость в цепочках if/elif при рендеринге.
    """

    URGENT_IMPORTANT = "urgent_important"
    NOT_URGENT_IMPORTANT = "not_urgent_important"
    URGENT_NOT_IMPORTANT = "urgent_not_important"
    NOT_URGENT_NOT_IMPORTANT = "not_urgent_not_important"

    @classmethod
    def from_flags(cls, is_urgent: bool, is_important: bool) -> Self:
        """Определяет квадрант по флагам срочности и важности."""
        match (is_urgent, is_important):
            case (True, True):
                return cls.URGENT_IMPORTANT
            case (False, True):
                return cls.NOT_URGENT_IMPORTANT
            case (True, False):
                return cls.URGENT_NOT_IMPORTANT
            case _:
                return cls.NOT_URGENT_NOT_IMPORTANT

    @property
    def title(self) -> str:
        """Человекочитаемое название квадранта."""
        titles: dict[Quadrant, str] = {
            Quadrant.URGENT_IMPORTANT: "Срочно и Важно — Делать сейчас",
            Quadrant.NOT_URGENT_IMPORTANT: "Важно, но не срочно — Планировать",
            Quadrant.URGENT_NOT_IMPORTANT: "Срочно, но не важно — Делегировать",
            Quadrant.NOT_URGENT_NOT_IMPORTANT: "Не срочно и не важно — Удалить/Отложить",
        }
        return titles[self]

    @property
    def icon(self) -> str:
        """Эмодзи-индикатор квадранта."""
        icons: dict[Quadrant, str] = {
            Quadrant.URGENT_IMPORTANT: "🔴",
            Quadrant.NOT_URGENT_IMPORTANT: "🟦",
            Quadrant.URGENT_NOT_IMPORTANT: "🟨",
            Quadrant.NOT_URGENT_NOT_IMPORTANT: "🟩",
        }
        return icons[self]

    @property
    def style(self) -> str:
        """Rich-стиль панели для квадранта."""
        styles: dict[Quadrant, str] = {
            Quadrant.URGENT_IMPORTANT: Theme.URGENT_IMPORTANT,
            Quadrant.NOT_URGENT_IMPORTANT: Theme.NOT_URGENT_IMPORTANT,
            Quadrant.URGENT_NOT_IMPORTANT: Theme.URGENT_NOT_IMPORTANT,
            Quadrant.NOT_URGENT_NOT_IMPORTANT: Theme.NOT_URGENT_NOT_IMPORTANT,
        }
        return styles[self]

    @property
    def border_color(self) -> str:
        """Цвет рамки панели квадранта."""
        colors: dict[Quadrant, str] = {
            Quadrant.URGENT_IMPORTANT: "red3",
            Quadrant.NOT_URGENT_IMPORTANT: "blue3",
            Quadrant.URGENT_NOT_IMPORTANT: "yellow3",
            Quadrant.NOT_URGENT_NOT_IMPORTANT: "green3",
        }
        return colors[self]


class SortField(StrEnum):
    """Доступные поля сортировки задач."""

    ID = auto()
    TITLE = auto()
    IMPORTANCE = auto()
    URGENCY = auto()
    CREATED_AT = auto()
    COMPLETED_AT = auto()


class FilterMode(StrEnum):
    """Доступные режимы фильтрации задач."""

    ALL = auto()
    COMPLETED = auto()
    ACTIVE = auto()
    IMPORTANT = auto()
    URGENT = auto()
    URGENT_IMPORTANT = auto()
    NOT_URGENT_IMPORTANT = auto()
    URGENT_NOT_IMPORTANT = auto()
    NOT_URGENT_NOT_IMPORTANT = auto()


@dataclass(slots=True)
class Task:
    """Доменная сущность задачи.

    Attributes:
        id: Уникальный идентификатор задачи (UUID4, укороченный).
        title: Название задачи.
        description: Подробное описание задачи.
        is_urgent: Флаг срочности.
        is_important: Флаг важности.
        is_completed: Флаг выполнения.
        created_at: Дата и время создания.
        completed_at: Дата и время выполнения (если выполнена).
    """

    id: str
    title: str
    description: str
    is_urgent: bool
    is_important: bool
    is_completed: bool = False
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None

    @property
    def quadrant(self) -> Quadrant:
        """Квадрант Матрицы Эйзенхауэра, которому принадлежит задача."""
        return Quadrant.from_flags(self.is_urgent, self.is_important)

    @property
    def status_icon(self) -> str:
        """Иконка текущего статуса задачи."""
        return "✅" if self.is_completed else "⏳"

    def mark_completed(self) -> None:
        """Помечает задачу выполненной и фиксирует время завершения."""
        self.is_completed = True
        self.completed_at = datetime.now()

    def mark_active(self) -> None:
        """Снимает отметку о выполнении задачи."""
        self.is_completed = False
        self.completed_at = None

    def to_dict(self) -> dict[str, Any]:
        """Сериализует задачу в словарь, пригодный для JSON."""
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        data["completed_at"] = (
            self.completed_at.isoformat() if self.completed_at else None
        )
        return data

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Self:
        """Десериализует задачу из словаря, полученного из JSON."""
        return cls(
            id=str(payload["id"]),
            title=str(payload["title"]),
            description=str(payload.get("description", "")),
            is_urgent=bool(payload["is_urgent"]),
            is_important=bool(payload["is_important"]),
            is_completed=bool(payload.get("is_completed", False)),
            created_at=datetime.fromisoformat(payload["created_at"]),
            completed_at=(
                datetime.fromisoformat(payload["completed_at"])
                if payload.get("completed_at")
                else None
            ),
        )


# ======================================================================================
# ИСКЛЮЧЕНИЯ ДОМЕННОГО СЛОЯ
# ======================================================================================


class TaskNotFoundError(Exception):
    """Возникает при попытке обратиться к несуществующей задаче."""


class ValidationError(Exception):
    """Возникает при ошибке валидации пользовательского ввода."""


class StorageError(Exception):
    """Возникает при ошибках чтения/записи файлов хранилища."""


# ======================================================================================
# SERVICE: ВАЛИДАЦИЯ
# ======================================================================================


class ValidationService:
    """Единая точка валидации пользовательского ввода.

    Исключает дублирование проверок по всему приложению.
    """

    _TRUE_TOKENS = {"y", "yes", "1", "д", "да"}
    _FALSE_TOKENS = {"n", "no", "0", "н", "нет"}

    @staticmethod
    def validate_non_empty_text(value: str, field_name: str) -> str:
        """Проверяет, что строка не пустая и не состоит из пробелов.

        Args:
            value: Введённое пользователем значение.
            field_name: Название поля для текста ошибки.

        Returns:
            Очищенная от лишних пробелов строка.

        Raises:
            ValidationError: Если строка пустая после очистки.
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValidationError(f"Поле «{field_name}» не может быть пустым.")
        return cleaned

    @classmethod
    def parse_boolean(cls, value: str) -> bool:
        """Преобразует свободный пользовательский ввод в булево значение.

        Поддерживает варианты: y/Y/yes/YES/1/д/Д/да/Да для True
        и n/N/no/NO/0/н/Н/нет/Нет для False, без учёта регистра.

        Raises:
            ValidationError: Если значение не распознано.
        """
        normalized = value.strip().lower()
        if normalized in cls._TRUE_TOKENS:
            return True
        if normalized in cls._FALSE_TOKENS:
            return False
        raise ValidationError(
            "Ожидается ответ да/нет (y/n, д/н, yes/no, 1/0)."
        )

    @staticmethod
    def parse_positive_int(value: str, field_name: str = "значение") -> int:
        """Преобразует строку в положительное целое число.

        Raises:
            ValidationError: Если строка не является числом или число <= 0.
        """
        cleaned = value.strip()
        if not cleaned.isdigit():
            raise ValidationError(
                f"«{field_name}» должно быть положительным целым числом."
            )
        number = int(cleaned)
        if number <= 0:
            raise ValidationError(f"«{field_name}» должно быть больше нуля.")
        return number

    @staticmethod
    def validate_task_id(value: str) -> str:
        """Проверяет и нормализует идентификатор задачи."""
        cleaned = value.strip()
        if not cleaned:
            raise ValidationError("ID задачи не может быть пустым.")
        return cleaned


# ======================================================================================
# SERVICE: РЕЗЕРВНОЕ КОПИРОВАНИЕ
# ======================================================================================


class BackupService:
    """Управляет резервными копиями файла хранилища.

    Хранит ограниченное число последних копий, автоматически удаляя
    самые старые при превышении лимита.
    """

    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        self._config = config
        self._logger = logger
        self._config.backup_dir.mkdir(parents=True, exist_ok=True)

    def create_backup(self, source: Path, *, reason: str = "routine") -> Path | None:
        """Создаёт резервную копию исходного файла, если он существует.

        Args:
            source: Путь к файлу, который нужно скопировать.
            reason: Причина бэкапа (используется в имени файла и логах).

        Returns:
            Путь к созданной копии или None, если исходный файл отсутствует.
        """
        if not source.exists():
            return None
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_name = f"{source.stem}_{reason}_{timestamp}{source.suffix}.bak"
        backup_path = self._config.backup_dir / backup_name
        try:
            shutil.copy2(source, backup_path)
            self._logger.info("Создана резервная копия: %s", backup_path)
        except OSError as exc:
            self._logger.error("Не удалось создать резервную копию: %s", exc)
            return None
        self._rotate_backups()
        return backup_path

    def _rotate_backups(self) -> None:
        """Удаляет старые резервные копии сверх установленного лимита."""
        backups = sorted(
            self._config.backup_dir.glob("*.bak"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale_backup in backups[self._config.max_backups :]:
            try:
                stale_backup.unlink()
                self._logger.debug("Удалена устаревшая копия: %s", stale_backup)
            except OSError as exc:
                self._logger.warning("Не удалось удалить копию %s: %s", stale_backup, exc)

    def list_backups(self) -> list[Path]:
        """Возвращает список существующих резервных копий, новые сначала."""
        return sorted(
            self._config.backup_dir.glob("*.bak"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )


# ======================================================================================
# SERVICE: ХРАНИЛИЩЕ (REPOSITORY)
# ======================================================================================


class TaskRepository:
    """Отвечает за чтение и запись задач в JSON-хранилище.

    Реализует устойчивость к повреждённому JSON: при сбое десериализации
    повреждённый файл переносится в резервную копию, а вместо него
    создаётся новый пустой файл хранилища.
    """

    def __init__(
        self,
        config: AppConfig,
        backup_service: BackupService,
        logger: logging.Logger,
    ) -> None:
        self._config = config
        self._backup_service = backup_service
        self._logger = logger
        self.last_saved_at: datetime | None = None

    def load(self) -> list[Task]:
        """Загружает список задач из файла хранилища.

        Returns:
            Список объектов Task. Пустой список, если файл отсутствует
            или был повреждён (в этом случае создаётся новый файл).
        """
        path = self._config.data_file
        if not path.exists():
            self._logger.info("Файл хранилища %s не найден, будет создан новый.", path)
            self._write_raw([])
            return []

        try:
            raw_text = path.read_text(encoding="utf-8")
            payload = json.loads(raw_text)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            self._logger.error("Файл хранилища повреждён: %s", exc)
            self._quarantine_corrupted_file()
            return []

        try:
            tasks_payload = payload.get("tasks", []) if isinstance(payload, dict) else payload
            tasks = [Task.from_dict(item) for item in tasks_payload]
            self._logger.info("Загружено задач: %d", len(tasks))
            return tasks
        except (KeyError, ValueError, TypeError) as exc:
            self._logger.error("Ошибка структуры данных в JSON: %s", exc)
            self._quarantine_corrupted_file()
            return []

    def _quarantine_corrupted_file(self) -> None:
        """Изолирует повреждённый файл в backups и создаёт новый чистый файл."""
        self._backup_service.create_backup(self._config.data_file, reason="corrupted")
        self._write_raw([])
        self._logger.warning(
            "Поврежденный файл сохранён в резервной копии, создано новое хранилище."
        )

    def save(self, tasks: list[Task]) -> None:
        """Сохраняет список задач в JSON-файл с предварительным бэкапом.

        Raises:
            StorageError: Если запись на диск не удалась.
        """
        self._backup_service.create_backup(self._config.data_file, reason="presave")
        self._write_raw(tasks)
        self.last_saved_at = datetime.now()
        self._logger.info("Сохранено задач: %d", len(tasks))

    def _write_raw(self, tasks: list[Task]) -> None:
        """Низкоуровневая запись задач на диск в формате версии схемы."""
        payload = {
            "schema_version": self._config.json_schema_version,
            "saved_at": datetime.now().isoformat(),
            "tasks": [task.to_dict() for task in tasks],
        }
        try:
            self._config.data_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=self._config.json_indent),
                encoding="utf-8",
            )
        except OSError as exc:
            self._logger.critical("Не удалось записать файл хранилища: %s", exc)
            raise StorageError(f"Ошибка записи файла: {exc}") from exc

    def export_to(self, destination: Path, tasks: list[Task]) -> None:
        """Экспортирует задачи во внешний JSON-файл с метаданными версии."""
        payload = {
            "schema_version": self._config.json_schema_version,
            "exported_at": datetime.now().isoformat(),
            "app_version": CONFIG.app_version,
            "tasks": [task.to_dict() for task in tasks],
        }
        try:
            destination.write_text(
                json.dumps(payload, ensure_ascii=False, indent=self._config.json_indent),
                encoding="utf-8",
            )
        except OSError as exc:
            raise StorageError(f"Ошибка экспорта: {exc}") from exc

    def import_from(self, source: Path) -> list[Task]:
        """Импортирует задачи из внешнего JSON-файла с проверкой версии схемы.

        Raises:
            StorageError: Если файл не найден, повреждён или версия схемы
                несовместима.
        """
        if not source.exists():
            raise StorageError(f"Файл {source} не найден.")
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise StorageError(f"Не удалось прочитать файл импорта: {exc}") from exc

        schema_version = payload.get("schema_version")
        if schema_version is None or schema_version > self._config.json_schema_version:
            raise StorageError(
                f"Несовместимая версия схемы файла импорта: {schema_version!r}."
            )

        try:
            return [Task.from_dict(item) for item in payload.get("tasks", [])]
        except (KeyError, ValueError, TypeError) as exc:
            raise StorageError(f"Некорректная структура файла импорта: {exc}") from exc


# ======================================================================================
# SERVICE: ИСТОРИЯ ДЕЙСТВИЙ (UNDO/REDO)
# ======================================================================================


class HistoryService:
    """Хранит снимки состояния списка задач для отмены/повтора действий.

    Реализован через два ограниченных стека (deque), что даёт O(1)
    операции отмены и повтора без неограниченного роста памяти.
    """

    def __init__(self, limit: int) -> None:
        self._undo_stack: deque[list[Task]] = deque(maxlen=limit)
        self._redo_stack: deque[list[Task]] = deque(maxlen=limit)

    @staticmethod
    def _snapshot(tasks: list[Task]) -> list[Task]:
        """Создаёт глубокую неизменяемую копию текущего списка задач."""
        return [replace(task) for task in tasks]

    def record(self, tasks: list[Task]) -> None:
        """Фиксирует текущее состояние перед изменением и очищает redo."""
        self._undo_stack.append(self._snapshot(tasks))
        self._redo_stack.clear()

    def undo(self, current: list[Task]) -> list[Task] | None:
        """Возвращает предыдущее состояние списка задач, если оно есть."""
        if not self._undo_stack:
            return None
        self._redo_stack.append(self._snapshot(current))
        return self._undo_stack.pop()

    def redo(self, current: list[Task]) -> list[Task] | None:
        """Возвращает отменённое ранее состояние списка задач, если оно есть."""
        if not self._redo_stack:
            return None
        self._undo_stack.append(self._snapshot(current))
        return self._redo_stack.pop()

    @property
    def can_undo(self) -> bool:
        """Доступна ли отмена действия."""
        return bool(self._undo_stack)

    @property
    def can_redo(self) -> bool:
        """Доступен ли повтор отменённого действия."""
        return bool(self._redo_stack)


# ======================================================================================
# SERVICE: СТАТИСТИКА
# ======================================================================================


@dataclass(slots=True, frozen=True)
class DashboardStats:
    """Снимок агрегированной статистики по задачам."""

    total: int
    completed: int
    active: int
    completion_rate: float
    quadrant_counts: dict[Quadrant, int]
    busiest_quadrant: Quadrant | None
    overdue_important_count: int
    average_per_quadrant: float
    last_saved_at: datetime | None
    last_launch_at: datetime


class StatisticsService:
    """Вычисляет статистику и метрики продуктивности по задачам."""

    @staticmethod
    def compute(
        tasks: list[Task],
        last_saved_at: datetime | None,
        last_launch_at: datetime,
    ) -> DashboardStats:
        """Строит полный снимок статистики на основе текущих задач."""
        total = len(tasks)
        completed = sum(1 for task in tasks if task.is_completed)
        active = total - completed
        completion_rate = (completed / total * 100) if total else 0.0

        quadrant_counts: dict[Quadrant, int] = {quadrant: 0 for quadrant in Quadrant}
        for task in tasks:
            quadrant_counts[task.quadrant] += 1

        busiest_quadrant = (
            max(quadrant_counts, key=lambda quadrant: quadrant_counts[quadrant])
            if total
            else None
        )

        overdue_important_count = sum(
            1
            for task in tasks
            if task.is_important and task.is_urgent and not task.is_completed
        )

        average_per_quadrant = total / len(Quadrant) if total else 0.0

        return DashboardStats(
            total=total,
            completed=completed,
            active=active,
            completion_rate=completion_rate,
            quadrant_counts=quadrant_counts,
            busiest_quadrant=busiest_quadrant,
            overdue_important_count=overdue_important_count,
            average_per_quadrant=average_per_quadrant,
            last_saved_at=last_saved_at,
            last_launch_at=last_launch_at,
        )


# ======================================================================================
# SERVICE: TASK MANAGER (ФАСАД БИЗНЕС-ЛОГИКИ)
# ======================================================================================


class TaskManager:
    """Главный сервис управления задачами.

    Инкапсулирует все операции над коллекцией задач: создание, удаление,
    редактирование, поиск, фильтрацию, сортировку, отметку выполнения,
    статистику, импорт/экспорт и историю изменений. UI-слой обращается
    исключительно к этому классу и никогда не работает с Task напрямую
    в обход бизнес-правил.
    """

    def __init__(
        self,
        repository: TaskRepository,
        history: HistoryService,
        validator: ValidationService,
        logger: logging.Logger,
    ) -> None:
        self._repository = repository
        self._history = history
        self._validator = validator
        self._logger = logger
        self._tasks: list[Task] = []
        self.launched_at: datetime = datetime.now()

    # ----- загрузка / сохранение --------------------------------------------------

    def load(self) -> None:
        """Загружает задачи из хранилища в память."""
        self._tasks = self._repository.load()
        self._logger.info("TaskManager: загружено %d задач.", len(self._tasks))

    def save(self) -> None:
        """Сохраняет текущее состояние задач в хранилище.

        Raises:
            StorageError: Если запись не удалась.
        """
        self._repository.save(self._tasks)

    @property
    def last_saved_at(self) -> datetime | None:
        """Время последнего успешного сохранения."""
        return self._repository.last_saved_at

    # ----- CRUD ----------------------------------------------------------------

    def create_task(
        self, title: str, description: str, is_urgent: bool, is_important: bool
    ) -> Task:
        """Создаёт новую задачу и добавляет её в коллекцию."""
        self._history.record(self._tasks)
        task = Task(
            id=self._generate_id(),
            title=self._validator.validate_non_empty_text(title, "Название"),
            description=description.strip(),
            is_urgent=is_urgent,
            is_important=is_important,
        )
        self._tasks.append(task)
        self._logger.info("Создана задача [%s] «%s».", task.id, task.title)
        return task

    def delete_task(self, task_id: str) -> Task:
        """Удаляет задачу по идентификатору.

        Raises:
            TaskNotFoundError: Если задача не найдена.
        """
        task = self.get_task(task_id)
        self._history.record(self._tasks)
        self._tasks.remove(task)
        self._logger.info("Удалена задача [%s] «%s».", task.id, task.title)
        return task

    def edit_task(
        self,
        task_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        is_urgent: bool | None = None,
        is_important: bool | None = None,
        is_completed: bool | None = None,
    ) -> Task:
        """Редактирует переданные поля задачи, остальные не изменяются.

        Raises:
            TaskNotFoundError: Если задача не найдена.
        """
        task = self.get_task(task_id)
        self._history.record(self._tasks)

        if title is not None:
            task.title = self._validator.validate_non_empty_text(title, "Название")
        if description is not None:
            task.description = description.strip()
        if is_urgent is not None:
            task.is_urgent = is_urgent
        if is_important is not None:
            task.is_important = is_important
        if is_completed is not None:
            task.mark_completed() if is_completed else task.mark_active()

        self._logger.info("Отредактирована задача [%s] «%s».", task.id, task.title)
        return task

    def complete_task(self, task_id: str) -> Task:
        """Отмечает задачу как выполненную.

        Raises:
            TaskNotFoundError: Если задача не найдена.
        """
        task = self.get_task(task_id)
        self._history.record(self._tasks)
        task.mark_completed()
        self._logger.info("Выполнена задача [%s] «%s».", task.id, task.title)
        return task

    def get_task(self, task_id: str) -> Task:
        """Возвращает задачу по идентификатору.

        Raises:
            TaskNotFoundError: Если задача с таким ID отсутствует.
        """
        normalized_id = self._validator.validate_task_id(task_id)
        for task in self._tasks:
            if task.id == normalized_id:
                return task
        raise TaskNotFoundError(f"Задача с ID «{normalized_id}» не найдена.")

    @property
    def tasks(self) -> list[Task]:
        """Полный список задач (только для чтения снаружи)."""
        return list(self._tasks)

    @property
    def count(self) -> int:
        """Количество задач в коллекции."""
        return len(self._tasks)

    # ----- поиск / фильтрация / сортировка --------------------------------------

    def search(self, query: str) -> list[Task]:
        """Ищет задачи по названию, описанию или ID без учёта регистра."""
        normalized_query = query.strip().lower()
        if not normalized_query:
            return []
        self._logger.info("Поиск по запросу «%s».", normalized_query)
        return [
            task
            for task in self._tasks
            if normalized_query in task.title.lower()
            or normalized_query in task.description.lower()
            or normalized_query in task.id.lower()
        ]

    def filter_tasks(self, mode: FilterMode) -> list[Task]:
        """Фильтрует задачи согласно выбранному режиму."""
        predicates: dict[FilterMode, Callable[[Task], bool]] = {
            FilterMode.ALL: lambda task: True,
            FilterMode.COMPLETED: lambda task: task.is_completed,
            FilterMode.ACTIVE: lambda task: not task.is_completed,
            FilterMode.IMPORTANT: lambda task: task.is_important,
            FilterMode.URGENT: lambda task: task.is_urgent,
            FilterMode.URGENT_IMPORTANT: lambda task: task.quadrant
            == Quadrant.URGENT_IMPORTANT,
            FilterMode.NOT_URGENT_IMPORTANT: lambda task: task.quadrant
            == Quadrant.NOT_URGENT_IMPORTANT,
            FilterMode.URGENT_NOT_IMPORTANT: lambda task: task.quadrant
            == Quadrant.URGENT_NOT_IMPORTANT,
            FilterMode.NOT_URGENT_NOT_IMPORTANT: lambda task: task.quadrant
            == Quadrant.NOT_URGENT_NOT_IMPORTANT,
        }
        predicate = predicates[mode]
        return [task for task in self._tasks if predicate(task)]

    def sort_tasks(self, tasks: list[Task], field_: SortField, *, reverse: bool = False) -> list[Task]:
        """Сортирует переданный список задач по выбранному полю."""
        key_functions: dict[SortField, Callable[[Task], Any]] = {
            SortField.ID: lambda task: task.id,
            SortField.TITLE: lambda task: task.title.lower(),
            SortField.IMPORTANCE: lambda task: task.is_important,
            SortField.URGENCY: lambda task: task.is_urgent,
            SortField.CREATED_AT: lambda task: task.created_at,
            SortField.COMPLETED_AT: lambda task: task.completed_at or datetime.min,
        }
        key_function = key_functions[field_]
        return sorted(tasks, key=key_function, reverse=reverse)

    def tasks_by_quadrant(self, quadrant: Quadrant) -> list[Task]:
        """Возвращает задачи, принадлежащие конкретному квадранту."""
        return [task for task in self._tasks if task.quadrant == quadrant]

    # ----- статистика -------------------------------------------------------------

    def get_statistics(self) -> DashboardStats:
        """Возвращает агрегированную статистику по текущим задачам."""
        return StatisticsService.compute(
            self._tasks, self.last_saved_at, self.launched_at
        )

    # ----- история -----------------------------------------------------------------

    def undo(self) -> bool:
        """Откатывает последнее изменение коллекции задач."""
        snapshot = self._history.undo(self._tasks)
        if snapshot is None:
            return False
        self._tasks = snapshot
        self._logger.info("Выполнена отмена последнего действия.")
        return True

    def redo(self) -> bool:
        """Повторяет последнее отменённое изменение коллекции задач."""
        snapshot = self._history.redo(self._tasks)
        if snapshot is None:
            return False
        self._tasks = snapshot
        self._logger.info("Выполнен повтор отменённого действия.")
        return True

    @property
    def can_undo(self) -> bool:
        """Доступна ли отмена."""
        return self._history.can_undo

    @property
    def can_redo(self) -> bool:
        """Доступен ли повтор."""
        return self._history.can_redo

    # ----- импорт / экспорт ---------------------------------------------------------

    def export_tasks(self, destination: Path) -> int:
        """Экспортирует текущие задачи в указанный файл.

        Returns:
            Количество экспортированных задач.
        """
        self._repository.export_to(destination, self._tasks)
        self._logger.info("Экспортировано %d задач в %s.", len(self._tasks), destination)
        return len(self._tasks)

    def import_tasks(self, source: Path, *, replace_existing: bool) -> int:
        """Импортирует задачи из файла, заменяя или дополняя текущие.

        Returns:
            Количество импортированных задач.
        """
        imported_tasks = self._repository.import_from(source)
        self._history.record(self._tasks)
        if replace_existing:
            self._tasks = imported_tasks
        else:
            existing_ids = {task.id for task in self._tasks}
            self._tasks.extend(
                task for task in imported_tasks if task.id not in existing_ids
            )
        self._logger.info("Импортировано %d задач из %s.", len(imported_tasks), source)
        return len(imported_tasks)

    # ----- внутреннее -----------------------------------------------------------------

    @staticmethod
    def _generate_id() -> str:
        """Генерирует короткий уникальный идентификатор задачи."""
        return uuid.uuid4().hex[:8]


# ======================================================================================
# UI: ЦЕНТР УВЕДОМЛЕНИЙ
# ======================================================================================


class NotificationCenter:
    """Единая точка вывода всех уведомлений приложения.

    Гарантирует визуально согласованный стиль для успеха, ошибки,
    предупреждения и информационных сообщений по всему приложению.
    """

    def __init__(self, console: Console) -> None:
        self._console = console

    def success(self, message: str, *, title: str = "Успешно") -> None:
        """Выводит зелёную панель с сообщением об успехе."""
        panel = Panel(
            Align.center(Text(f"✅ {message}", style="bold white")),
            title=f"[bold green]{title}[/bold green]",
            border_style="green",
            box=CONFIG.panel_box,
            padding=(1, 4),
        )
        self._console.print(panel)

    def error(self, message: str, *, title: str = "Ошибка") -> None:
        """Выводит красную панель с сообщением об ошибке."""
        panel = Panel(
            Align.center(Text(f"❌ {message}", style="bold white")),
            title=f"[bold red]{title}[/bold red]",
            border_style="red",
            box=CONFIG.panel_box,
            padding=(1, 4),
        )
        self._console.print(panel)

    def warning(self, message: str, *, title: str = "Внимание") -> None:
        """Выводит жёлтую панель с предупреждением."""
        panel = Panel(
            Align.center(Text(f"⚠️ {message}", style="bold black")),
            title=f"[bold yellow]{title}[/bold yellow]",
            border_style="yellow",
            box=CONFIG.panel_box,
            padding=(1, 4),
        )
        self._console.print(panel)

    def info(self, message: str, *, title: str = "Информация") -> None:
        """Выводит синюю панель с информационным сообщением."""
        panel = Panel(
            Align.center(Text(f"ℹ️ {message}", style="bold white")),
            title=f"[bold blue]{title}[/bold blue]",
            border_style="blue",
            box=CONFIG.panel_box,
            padding=(1, 4),
        )
        self._console.print(panel)


# ======================================================================================
# UI: МЕНЮ КАК ДАННЫЕ
# ======================================================================================


@dataclass(slots=True, frozen=True)
class MenuItem:
    """Описание одного пункта меню: ключ, текст, иконка и обработчик."""

    key: str
    icon: str
    label: str
    handler: Callable[[], None]


# ======================================================================================
# UI: КОНСОЛЬНЫЙ ИНТЕРФЕЙС
# ======================================================================================


class ConsoleUI:
    """Слой взаимодействия с пользователем.

    Не содержит бизнес-логики: все операции делегируются в TaskManager,
    UI отвечает исключительно за отображение данных и сбор ввода.
    """

    def __init__(self, manager: TaskManager, config: AppConfig, logger: logging.Logger) -> None:
        self._console = Console()
        self._manager = manager
        self._config = config
        self._logger = logger
        self._notify = NotificationCenter(self._console)
        self._validator = ValidationService()
        self._running = True
        self._menu_items: list[MenuItem] = self._build_menu()

    # ----- построение меню из данных, а не if/elif -----------------------------------

    def _build_menu(self) -> list[MenuItem]:
        """Строит структуру меню в виде списка данных."""
        return [
            MenuItem("1", "📝", "Добавить задачу", self._handle_add_task),
            MenuItem("2", "📊", "Матрица Эйзенхауэра", self._handle_show_matrix),
            MenuItem("3", "✏️", "Редактировать задачу", self._handle_edit_task),
            MenuItem("4", "✅", "Выполнить задачу", self._handle_complete_task),
            MenuItem("5", "🗑", "Удалить задачу", self._handle_delete_task),
            MenuItem("6", "🔍", "Поиск задач", self._handle_search),
            MenuItem("7", "📂", "Фильтр и сортировка", self._handle_filter),
            MenuItem("8", "📈", "Статистика (Dashboard)", self._handle_dashboard),
            MenuItem("9", "↩️", "Отменить действие (Undo)", self._handle_undo),
            MenuItem("10", "↪️", "Повторить действие (Redo)", self._handle_redo),
            MenuItem("11", "📤", "Экспорт задач", self._handle_export),
            MenuItem("12", "📥", "Импорт задач", self._handle_import),
            MenuItem("13", "⚙", "Настройки и резервные копии", self._handle_settings),
            MenuItem("14", "💾", "Сохранить", self._handle_save),
            MenuItem("0", "❌", "Выход", self._handle_exit),
        ]

    # ----- точка входа -----------------------------------------------------------------

    def run(self) -> None:
        """Запускает главный цикл приложения."""
        self._logger.info("Приложение запущено.")
        try:
            self._load_with_spinner()
            self._show_welcome_screen()
            while self._running:
                self._show_main_menu()
                choice = Prompt.ask("[bold cyan]Выберите действие[/bold cyan]")
                self._dispatch(choice)
        except KeyboardInterrupt:
            self._console.print()
            self._notify.warning("Получен сигнал прерывания (Ctrl+C).")
            self._handle_exit()
        except Exception as exc:  # noqa: BLE001 - верхнеуровневый предохранитель
            self._logger.critical("Необработанная ошибка верхнего уровня: %s", exc)
            self._notify.error(f"Непредвиденная ошибка: {exc}")
        finally:
            self._logger.info("Приложение завершило работу.")

    def _dispatch(self, choice: str) -> None:
        """Находит и вызывает обработчик меню по ключу выбора."""
        normalized_choice = choice.strip()
        for item in self._menu_items:
            if item.key == normalized_choice:
                self._console.print()
                item.handler()
                return
        self._notify.error("Неверный пункт меню. Попробуйте снова.")

    # ----- загрузка ----------------------------------------------------------------

    def _load_with_spinner(self) -> None:
        """Загружает задачи, отображая анимацию ожидания."""
        with Status(
            "[bold cyan]Загрузка задач...[/bold cyan]", spinner="dots", console=self._console
        ):
            self._manager.load()

    # ----- приветственный экран -------------------------------------------------------

    def _show_welcome_screen(self) -> None:
        """Отображает приветственный экран приложения."""
        now = datetime.now()
        title_text = Text(f"🚀 {self._config.app_name}", style="bold cyan", justify="center")
        subtitle_text = Text(self._config.app_tagline, style="italic white", justify="center")

        info_table = Table.grid(padding=(0, 3))
        info_table.add_column(justify="center")
        info_table.add_column(justify="center")
        info_table.add_column(justify="center")
        info_table.add_row(
            f"📅 {now.strftime(self._config.short_date_format)}",
            f"🕐 {now.strftime('%H:%M:%S')}",
            f"📋 Загружено задач: {self._manager.count}",
        )

        body = Group(
            Align.center(title_text),
            Text(""),
            Align.center(subtitle_text),
            Text(""),
            Rule(style="cyan"),
            Align.center(info_table),
        )

        panel = Panel(
            Padding(body, (1, 2)),
            box=CONFIG.panel_box,
            border_style="bold cyan",
            title=f"[bold white]v{self._config.app_version}[/bold white]",
            subtitle="[dim]Clean Architecture · SOLID · Rich UI[/dim]",
        )
        self._console.print(panel)
        self._console.print()

    # ----- главное меню ---------------------------------------------------------------

    def _show_main_menu(self) -> None:
        """Отображает главное меню в виде таблицы Rich."""
        table = Table(
            box=self._config.table_box,
            border_style="bold magenta",
            title="[bold white]ГЛАВНОЕ МЕНЮ[/bold white]",
            title_style="bold magenta",
            show_lines=False,
            expand=True,
        )
        table.add_column("№", justify="center", style="bold cyan", width=4)
        table.add_column("", justify="center", width=4)
        table.add_column("Действие", style="white")

        for item in self._menu_items:
            table.add_row(item.key, item.icon, item.label)

        self._console.print(table)

    # ----- добавление задачи --------------------------------------------------------

    def _handle_add_task(self) -> None:
        """Пошаговый мастер создания новой задачи."""
        self._console.print(Rule("[bold cyan]📝 Новая задача[/bold cyan]"))
        try:
            title = Prompt.ask("[cyan]Название задачи[/cyan]")
            description = Prompt.ask("[cyan]Описание задачи[/cyan]", default="")
            is_urgent = self._ask_boolean("Задача срочная?")
            is_important = self._ask_boolean("Задача важная?")

            task = self._manager.create_task(title, description, is_urgent, is_important)
            self._autosave()
            self._notify.success(
                f"Задача «{task.title}» (ID: {task.id}) создана в квадранте "
                f"{task.quadrant.icon} {task.quadrant.title}."
            )
        except ValidationError as exc:
            self._notify.error(str(exc))

    def _ask_boolean(self, question: str) -> bool:
        """Запрашивает у пользователя ответ да/нет с повтором при ошибке."""
        while True:
            raw_answer = Prompt.ask(f"[cyan]{question}[/cyan] (y/n, д/н)")
            try:
                return self._validator.parse_boolean(raw_answer)
            except ValidationError as exc:
                self._notify.error(str(exc))

    # ----- матрица эйзенхауэра -------------------------------------------------------

    def _handle_show_matrix(self) -> None:
        """Отображает Матрицу Эйзенхауэра в виде сетки 2×2."""
        self._console.print(Rule("[bold cyan]📊 Матрица Эйзенхауэра[/bold cyan]"))
        top_row = Columns(
            [
                self._render_quadrant_panel(Quadrant.URGENT_IMPORTANT),
                self._render_quadrant_panel(Quadrant.NOT_URGENT_IMPORTANT),
            ],
            equal=True,
            expand=True,
        )
        bottom_row = Columns(
            [
                self._render_quadrant_panel(Quadrant.URGENT_NOT_IMPORTANT),
                self._render_quadrant_panel(Quadrant.NOT_URGENT_NOT_IMPORTANT),
            ],
            equal=True,
            expand=True,
        )
        self._console.print(top_row)
        self._console.print(bottom_row)

    def _render_quadrant_panel(self, quadrant: Quadrant) -> Panel:
        """Строит панель одного квадранта Матрицы Эйзенхауэра."""
        tasks = self._manager.tasks_by_quadrant(quadrant)
        if not tasks:
            content: Any = Align.center(Text("Нет задач", style="italic dim"))
        else:
            tree = Tree(f"[bold]{quadrant.icon} {len(tasks)} задач(и)[/bold]")
            for task in tasks:
                node_label = (
                    f"{task.status_icon} [bold]{task.title}[/bold] "
                    f"[dim](ID: {task.id})[/dim]\n"
                    f"   {task.description or '—'}\n"
                    f"   [dim]Создано: {task.created_at.strftime(self._config.short_date_format)}[/dim]"
                )
                tree.add(node_label)
            content = tree

        return Panel(
            content,
            title=f"[bold]{quadrant.icon} {quadrant.title}[/bold]",
            border_style=quadrant.border_color,
            box=self._config.heavy_box,
            padding=(1, 2),
        )

    # ----- редактирование --------------------------------------------------------------

    def _handle_edit_task(self) -> None:
        """Редактирует выбранную задачу по ID."""
        self._console.print(Rule("[bold cyan]✏️ Редактирование задачи[/bold cyan]"))
        try:
            task_id = Prompt.ask("[cyan]Введите ID задачи[/cyan]")
            task = self._manager.get_task(task_id)
            self._render_task_details(task)

            new_title = Prompt.ask("[cyan]Новое название[/cyan]", default=task.title)
            new_description = Prompt.ask(
                "[cyan]Новое описание[/cyan]", default=task.description
            )
            new_urgent = self._ask_boolean("Срочная?")
            new_important = self._ask_boolean("Важная?")
            new_completed = self._ask_boolean("Выполнена?")

            self._manager.edit_task(
                task_id,
                title=new_title,
                description=new_description,
                is_urgent=new_urgent,
                is_important=new_important,
                is_completed=new_completed,
            )
            self._autosave()
            self._notify.success(f"Задача «{new_title}» обновлена.")
        except TaskNotFoundError as exc:
            self._notify.error(str(exc))
        except ValidationError as exc:
            self._notify.error(str(exc))

    def _render_task_details(self, task: Task) -> None:
        """Отображает подробную информацию о задаче."""
        self._console.print(Pretty(task.to_dict()))

    # ----- выполнение --------------------------------------------------------------------

    def _handle_complete_task(self) -> None:
        """Отмечает задачу как выполненную."""
        self._console.print(Rule("[bold cyan]✅ Выполнение задачи[/bold cyan]"))
        try:
            task_id = Prompt.ask("[cyan]Введите ID задачи[/cyan]")
            task = self._manager.complete_task(task_id)
            self._autosave()
            self._notify.success(f"Задача «{task.title}» отмечена как выполненная.")
        except TaskNotFoundError as exc:
            self._notify.error(str(exc))

    # ----- удаление -----------------------------------------------------------------------

    def _handle_delete_task(self) -> None:
        """Удаляет задачу после подтверждения пользователем."""
        self._console.print(Rule("[bold red]🗑 Удаление задачи[/bold red]"))
        try:
            task_id = Prompt.ask("[cyan]Введите ID задачи[/cyan]")
            task = self._manager.get_task(task_id)
            self._render_task_details(task)
            confirmed = Confirm.ask(
                f"[bold red]Точно удалить задачу «{task.title}»?[/bold red]"
            )
            if not confirmed:
                self._notify.info("Удаление отменено.")
                return
            self._manager.delete_task(task_id)
            self._autosave()
            self._notify.success(f"Задача «{task.title}» удалена.")
        except TaskNotFoundError as exc:
            self._notify.error(str(exc))

    # ----- поиск ---------------------------------------------------------------------------

    def _handle_search(self) -> None:
        """Ищет задачи по тексту и подсвечивает совпадения."""
        self._console.print(Rule("[bold cyan]🔍 Поиск задач[/bold cyan]"))
        query = Prompt.ask("[cyan]Поисковый запрос[/cyan]")
        results = self._manager.search(query)
        if not results:
            self._notify.info("По вашему запросу ничего не найдено.")
            return
        self._render_task_table(results, highlight=query)

    def _render_task_table(self, tasks: list[Task], *, highlight: str = "") -> None:
        """Отрисовывает список задач в виде таблицы с опциональной подсветкой."""
        table = Table(box=self._config.table_box, border_style="cyan", expand=True)
        table.add_column("ID", style="bold cyan", width=10)
        table.add_column("Статус", justify="center", width=8)
        table.add_column("Название")
        table.add_column("Квадрант", justify="center")
        table.add_column("Создано", justify="center")

        for task in tasks:
            table.add_row(
                task.id,
                task.status_icon,
                self._highlighted_text(task.title, highlight),
                f"{task.quadrant.icon}",
                task.created_at.strftime(self._config.short_date_format),
            )
        self._console.print(table)

    @staticmethod
    def _highlighted_text(text_value: str, query: str) -> Text:
        """Подсвечивает в тексте все вхождения поискового запроса."""
        rich_text = Text(text_value)
        if query:
            rich_text.highlight_words([query], style="bold black on yellow")
        return rich_text

    # ----- фильтрация и сортировка -----------------------------------------------------------

    def _handle_filter(self) -> None:
        """Фильтрует и сортирует задачи по выбору пользователя."""
        self._console.print(Rule("[bold cyan]📂 Фильтр и сортировка[/bold cyan]"))

        filter_table = Table(box=box.SIMPLE, show_header=False)
        for index, mode in enumerate(FilterMode, start=1):
            filter_table.add_row(f"[cyan]{index}[/cyan]", mode.value)
        self._console.print(filter_table)

        filter_choice = Prompt.ask(
            "[cyan]Выберите фильтр[/cyan]",
            choices=[str(i) for i in range(1, len(FilterMode) + 1)],
            default="1",
        )
        selected_filter = list(FilterMode)[int(filter_choice) - 1]
        filtered = self._manager.filter_tasks(selected_filter)

        sort_table = Table(box=box.SIMPLE, show_header=False)
        for index, field_ in enumerate(SortField, start=1):
            sort_table.add_row(f"[cyan]{index}[/cyan]", field_.value)
        self._console.print(sort_table)

        sort_choice = Prompt.ask(
            "[cyan]Выберите поле сортировки[/cyan]",
            choices=[str(i) for i in range(1, len(SortField) + 1)],
            default="1",
        )
        selected_sort = list(SortField)[int(sort_choice) - 1]
        reverse = Confirm.ask("[cyan]Сортировать по убыванию?[/cyan]", default=False)

        result = self._manager.sort_tasks(filtered, selected_sort, reverse=reverse)
        if not result:
            self._notify.info("Нет задач, соответствующих выбранным условиям.")
            return
        self._render_task_table(result)

    # ----- dashboard --------------------------------------------------------------------------

    def _handle_dashboard(self) -> None:
        """Отображает живой дашборд статистики."""
        self._console.print(Rule("[bold cyan]📈 Dashboard статистики[/bold cyan]"))
        stats = self._manager.get_statistics()

        summary_table = Table(box=self._config.table_box, border_style="cyan", show_header=False)
        summary_table.add_column("Метрика", style="bold white")
        summary_table.add_column("Значение", style="bold cyan", justify="right")
        summary_table.add_row("Всего задач", str(stats.total))
        summary_table.add_row("Выполнено", str(stats.completed))
        summary_table.add_row("Осталось", str(stats.active))
        summary_table.add_row("Просроченных важных", str(stats.overdue_important_count))
        summary_table.add_row(
            "Среднее на квадрант", f"{stats.average_per_quadrant:.1f}"
        )
        summary_table.add_row(
            "Самый загруженный квадрант",
            f"{stats.busiest_quadrant.icon} {stats.busiest_quadrant.title}"
            if stats.busiest_quadrant
            else "—",
        )
        summary_table.add_row(
            "Последнее сохранение",
            stats.last_saved_at.strftime(self._config.date_format)
            if stats.last_saved_at
            else "Ещё не сохранялось",
        )
        summary_table.add_row(
            "Последний запуск", stats.last_launch_at.strftime(self._config.date_format)
        )

        progress = Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[bold white]Прогресс выполнения[/bold white]"),
            BarColumn(bar_width=40, complete_style="green", finished_style="green"),
            TaskProgressColumn(),
            console=self._console,
        )
        progress_task_id = progress.add_task("progress", total=100)
        progress.update(progress_task_id, completed=stats.completion_rate)

        quadrant_cards = [
            Panel(
                Align.center(
                    Text(
                        f"{quadrant.icon}\n{count}",
                        justify="center",
                        style="bold white",
                    )
                ),
                title=quadrant.title.split(" — ")[0],
                border_style=quadrant.border_color,
                box=CONFIG.panel_box,
            )
            for quadrant, count in stats.quadrant_counts.items()
        ]

        self._console.print(summary_table)
        self._console.print()
        self._console.print(progress)
        self._console.print()
        self._console.print(Columns(quadrant_cards, equal=True, expand=True))

    # ----- undo / redo --------------------------------------------------------------------------

    def _handle_undo(self) -> None:
        """Отменяет последнее изменение."""
        if self._manager.undo():
            self._autosave()
            self._notify.success("Последнее действие отменено.")
        else:
            self._notify.warning("Нет действий для отмены.")

    def _handle_redo(self) -> None:
        """Повторяет последнее отменённое изменение."""
        if self._manager.redo():
            self._autosave()
            self._notify.success("Действие повторено.")
        else:
            self._notify.warning("Нет действий для повтора.")

    # ----- экспорт / импорт --------------------------------------------------------------------

    def _handle_export(self) -> None:
        """Экспортирует задачи в указанный пользователем файл."""
        self._console.print(Rule("[bold cyan]📤 Экспорт задач[/bold cyan]"))
        destination_raw = Prompt.ask(
            "[cyan]Имя файла для экспорта[/cyan]", default="tasks_export.json"
        )
        try:
            destination = Path(destination_raw)
            count = self._manager.export_tasks(destination)
            self._notify.success(f"Экспортировано {count} задач в файл {destination}.")
        except StorageError as exc:
            self._notify.error(str(exc))

    def _handle_import(self) -> None:
        """Импортирует задачи из указанного пользователем файла."""
        self._console.print(Rule("[bold cyan]📥 Импорт задач[/bold cyan]"))
        source_raw = Prompt.ask("[cyan]Имя файла для импорта[/cyan]")
        replace_existing = Confirm.ask(
            "[cyan]Заменить текущие задачи импортированными (иначе — объединить)?[/cyan]",
            default=False,
        )
        try:
            source = Path(source_raw)
            count = self._manager.import_tasks(source, replace_existing=replace_existing)
            self._autosave()
            self._notify.success(f"Импортировано {count} задач из файла {source}.")
        except StorageError as exc:
            self._notify.error(str(exc))

    # ----- настройки ----------------------------------------------------------------------------

    def _handle_settings(self) -> None:
        """Отображает текущие настройки и список резервных копий."""
        self._console.print(Rule("[bold cyan]⚙ Настройки[/bold cyan]"))

        settings_table = Table(box=self._config.table_box, border_style="cyan", show_header=False)
        settings_table.add_column("Параметр", style="bold white")
        settings_table.add_column("Значение", style="cyan")
        settings_table.add_row("Файл данных", str(self._config.data_file))
        settings_table.add_row("Файл логов", str(self._config.log_file))
        settings_table.add_row("Папка резервных копий", str(self._config.backup_dir))
        settings_table.add_row("Лимит резервных копий", str(self._config.max_backups))
        settings_table.add_row("Лимит истории Undo/Redo", str(self._config.history_limit))
        settings_table.add_row("Версия приложения", self._config.app_version)
        settings_table.add_row("Версия схемы JSON", str(self._config.json_schema_version))
        self._console.print(settings_table)

        backup_service = BackupService(self._config, self._logger)
        backups = backup_service.list_backups()
        if backups:
            backup_tree = Tree("[bold]💾 Последние резервные копии[/bold]")
            for backup_path in backups[:10]:
                backup_tree.add(backup_path.name)
            self._console.print(backup_tree)
        else:
            self._notify.info("Резервные копии пока отсутствуют.")

    # ----- сохранение и выход --------------------------------------------------------------------

    def _handle_save(self) -> None:
        """Сохраняет задачи с прогресс-баром."""
        self._save_with_progress()
        self._notify.success("Все задачи сохранены на диск.")

    def _handle_exit(self) -> None:
        """Завершает приложение, сохраняя данные перед выходом."""
        try:
            self._save_with_progress()
        except StorageError as exc:
            self._notify.error(f"Не удалось сохранить перед выходом: {exc}")
        self._console.print(
            Panel(
                Align.center(Text("До встречи! 👋", style="bold cyan")),
                box=CONFIG.panel_box,
                border_style="cyan",
            )
        )
        self._running = False

    def _save_with_progress(self) -> None:
        """Сохраняет данные, отображая индикатор прогресса."""
        with Progress(
            SpinnerColumn(style="green"),
            TextColumn("[bold white]Сохранение данных...[/bold white]"),
            BarColumn(bar_width=30, complete_style="green"),
            console=self._console,
            transient=True,
        ) as progress:
            task_id = progress.add_task("saving", total=1)
            self._manager.save()
            progress.update(task_id, completed=1)

    def _autosave(self) -> None:
        """Автоматически сохраняет данные после изменяющих операций."""
        try:
            self._manager.save()
        except StorageError as exc:
            self._notify.error(f"Автосохранение не удалось: {exc}")


# ======================================================================================
# ТОЧКА ВХОДА
# ======================================================================================


def build_application() -> ConsoleUI:
    """Собирает граф зависимостей приложения (composition root)."""
    backup_service = BackupService(CONFIG, LOGGER)
    repository = TaskRepository(CONFIG, backup_service, LOGGER)
    history_service = HistoryService(CONFIG.history_limit)
    validator = ValidationService()
    manager = TaskManager(repository, history_service, validator, LOGGER)
    return ConsoleUI(manager, CONFIG, LOGGER)


def main() -> int:
    """Главная точка входа приложения.

    Returns:
        Код завершения процесса (0 — успех).
    """
    try:
        application = build_application()
        application.run()
        return 0
    except Exception as exc:  # noqa: BLE001 - последний рубеж защиты от падений
        LOGGER.critical("Фатальная ошибка при запуске приложения: %s", exc)
        Console().print(f"[bold red]Фатальная ошибка:[/bold red] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())