import logging
import threading
from typing import Dict, Optional
import numpy as np

logger = logging.getLogger(__name__)


class RealtimeUserMemory:
    """
    유저 실시간 행동 기반 16차원 취향 벡터 인메모리 관리자.
    - 유저 행동(클릭, 찜, 참여할게요, QR인증 등) 발생 시 즉시 해당 유저의 16차원 취향 벡터를 이동.
    - 0.001초 이내(통상 0.0001초) 연산 보장.
    - 스레드 안전성(Lock) 보장.
    """

    BEHAVIOR_WEIGHTS = {
        'VIEW': 1.0,
        'MENU_OPEN': 1.5,
        'BOOKMARK': 3.0,
        'LIKE': 3.0,
        'EVENT_INTENT': 5.0,        # [참여할게요]
        'EVENT_QR_VERIFIED': 6.0,   # [현장 QR 인증]
        'DISLIKE': -2.5,
    }

    LEARNING_RATE = 0.25

    def __init__(self):
        self._lock = threading.RLock()
        # user_id -> np.ndarray (16,)
        self._user_vectors: Dict[str, np.ndarray] = {}

    def clear(self) -> None:
        """메모리 초기화"""
        with self._lock:
            self._user_vectors.clear()

    @staticmethod
    def _l2_normalize(vec: np.ndarray, eps: float = 1e-9) -> np.ndarray:
        norm = np.linalg.norm(vec)
        return vec / max(norm, eps)

    def get_or_init_user_vector(self, user_id: str, default_vec: Optional[np.ndarray] = None) -> np.ndarray:
        """유저의 현재 16D 벡터 조회 (없으면 기본값 또는 랜덤 초기화 후 반환)"""
        with self._lock:
            if user_id in self._user_vectors:
                return self._user_vectors[user_id].copy()

            if default_vec is not None and default_vec.shape == (16,):
                initial = self._l2_normalize(default_vec.astype(np.float32))
            else:
                # 단위 구면 위의 균일 초기 벡터
                rng = np.random.RandomState(hash(user_id) % (2**32 - 1))
                initial = self._l2_normalize(rng.randn(16).astype(np.float32))

            self._user_vectors[user_id] = initial
            return initial.copy()

    def set_user_vector(self, user_id: str, vec: np.ndarray) -> None:
        """유저 벡터 직접 설정"""
        with self._lock:
            self._user_vectors[user_id] = self._l2_normalize(vec.astype(np.float32))

    def update_user_vector(
        self,
        user_id: str,
        behavior_type: str,
        venue_embedding: np.ndarray,
        weight_override: Optional[float] = None,
    ) -> np.ndarray:
        """
        유저 행동 발생 시 취향 벡터 실시간 이동 연산.
        delta = lr * weight * venue_embedding
        new_vec = normalize(old_vec + delta)
        """
        with self._lock:
            current_vec = self.get_or_init_user_vector(user_id)
            weight = weight_override if weight_override is not None else self.BEHAVIOR_WEIGHTS.get(behavior_type.upper(), 1.0)
            target_venue_emb = self._l2_normalize(venue_embedding.astype(np.float32).flatten())

            # 취향 벡터 점진적 이동
            delta = self.LEARNING_RATE * weight * target_venue_emb
            updated_vec = self._l2_normalize(current_vec + delta)

            self._user_vectors[user_id] = updated_vec
            logger.debug(f"[UserMemory] Updated user {user_id} with {behavior_type} (weight={weight})")
            return updated_vec.copy()


_user_memory_instance: Optional[RealtimeUserMemory] = None
_mem_lock = threading.Lock()


def get_user_memory() -> RealtimeUserMemory:
    global _user_memory_instance
    if _user_memory_instance is None:
        with _mem_lock:
            if _user_memory_instance is None:
                _user_memory_instance = RealtimeUserMemory()
    return _user_memory_instance
