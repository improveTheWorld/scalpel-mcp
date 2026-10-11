"""Watchdog file system watcher with event suppression registry for incremental graph updates."""

from pathlib import Path
import threading
import time
from typing import Dict, Optional
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import ServerConfig
from .indexer import Indexer


class SuppressionRegistry:
    """Thread-safe registry to suppress watcher events for files recently modified by the patcher."""

    def __init__(self, ttl_seconds: float = 0.5) -> None:
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._suppressed: Dict[str, float] = {}

    def suppress(self, path: Path | str) -> None:
        key = str(Path(path).resolve())
        with self._lock:
            self._suppressed[key] = time.time() + self.ttl

    def is_suppressed(self, path: Path | str) -> bool:
        key = str(Path(path).resolve())
        now = time.time()
        with self._lock:
            expiry = self._suppressed.get(key)
            if expiry is None:
                return False
            if now < expiry:
                return True
            del self._suppressed[key]
            return False


class WorkspaceEventHandler(FileSystemEventHandler):
    """Handles file modification events in workspace."""

    def __init__(
        self,
        config: ServerConfig,
        indexer: Indexer,
        suppression_registry: Optional[SuppressionRegistry] = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.indexer = indexer
        self.suppression = suppression_registry

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(str(event.src_path))
        if self.suppression and self.suppression.is_suppressed(path):
            return
        if not self.indexer.should_ignore(path):
            self.indexer.index_file(path)

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(str(event.src_path))
        if self.suppression and self.suppression.is_suppressed(path):
            return
        if not self.indexer.should_ignore(path):
            self.indexer.index_file(path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(str(event.src_path))
        # A file replaced by rename is reported as deleted while the new file is already
        # in place: removing it here left a live file out of the index (measured: README.md
        # after one edit_block). Only a path that is really gone leaves the index.
        if path.exists():
            return
        if not self.indexer.should_ignore(path):
            self.indexer.remove_file(path)


class WorkspaceWatcher:
    """Manages background watchdog observer."""

    def __init__(
        self,
        config: ServerConfig,
        indexer: Indexer,
        suppression_registry: Optional[SuppressionRegistry] = None,
    ) -> None:
        self.config = config
        self.indexer = indexer
        self.suppression = suppression_registry or SuppressionRegistry()
        self._observer: Optional[Observer] = None

    def start(self) -> None:
        """Start background filesystem observer."""
        handler = WorkspaceEventHandler(self.config, self.indexer, self.suppression)
        self._observer = Observer()
        self._observer.schedule(handler, str(self.config.workspace_root), recursive=True)
        self._observer.daemon = True
        self._observer.start()

    def stop(self) -> None:
        """Stop background observer cleanly."""
        if self._observer and self._observer.is_alive():
            self._observer.stop()
            self._observer.join(timeout=2.0)
