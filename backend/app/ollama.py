"""Local Ollama compatibility, admission and generation; never stores answers.

Capability metadata is read once per adapter lifetime (including failures).
Restart after changing Ollama/model/template/configuration. No probe generation.
"""

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

import httpx

from .grounding import SYSTEM_PROMPT, GeneratedAnswer, model_message


class ResponseProblem(Exception):
    def __init__(self, code: str, message: str, status: int = 503):
        self.code, self.message, self.status = code, message, status
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Capability:
    fast: bool
    reflection: bool
    boolean_think: bool = False
    reason: str = ""
    version: str | None = None

    def public(self):
        return {
            "fast": {
                "available": self.fast,
                "reason": "" if self.fast else self.reason,
            },
            "reflection": {
                "available": self.reflection,
                "reason": "" if self.reflection else self.reason,
            },
            "search": {"available": True, "reason": ""},
        }


class OllamaAdapter:
    def __init__(self, config, http):
        self.config, self._http = config, http
        self._capability = None
        self._capability_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(config.ask_concurrency)
        self._admitted = 0

    @asynccontextmanager
    async def admission(self):
        # No await between check/increment: atomic on the application's event loop.
        if self._admitted >= self.config.ask_concurrency + self.config.ask_queue_size:
            raise ResponseProblem(
                "QUEUE_FULL", "File d’attente pleine. Réessayez plus tard.", 429
            )
        self._admitted += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    self._slots.acquire(), self.config.ask_queue_timeout_seconds
                )
                acquired = True
            except TimeoutError as exc:
                raise ResponseProblem(
                    "QUEUE_TIMEOUT", "Attente trop longue. Aucune réponse produite."
                ) from exc
            yield
        finally:
            if acquired:
                self._slots.release()
            self._admitted -= 1

    async def capabilities(self):
        async with self._capability_lock:
            if self._capability is None:
                self._capability = await self._detect()
            return self._capability

    async def _detect(self):
        config = self.config
        version = None
        metadata = None
        model_missing = False
        # Bounded metadata-only calls. The model name alone never proves support.
        try:
            async with asyncio.timeout(5):
                response = await self._http().get(f"{config.ollama_url}/api/version")
                response.raise_for_status()
                value = response.json().get("version")
                if isinstance(value, str):
                    version = value
        except (httpx.HTTPError, ValueError, AttributeError, TimeoutError):
            pass
        try:
            async with asyncio.timeout(5):
                response = await self._http().post(
                    f"{config.ollama_url}/api/show", json={"model": config.ollama_model}
                )
                model_missing = response.status_code == 404
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict):
                    metadata = data
        except (httpx.HTTPError, ValueError, TimeoutError):
            pass
        if model_missing:
            return Capability(
                False,
                False,
                reason="Modèle configuré introuvable dans Ollama. Installez-le puis redémarrez le backend.",
                version=version,
            )
        if ":cloud" in config.ollama_model or (
            metadata and (metadata.get("remote_model") or metadata.get("remote_host"))
        ):
            return Capability(
                False, False, reason="Un modèle local est requis.", version=version
            )
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version or "")
        supported_version = bool(
            match and tuple(map(int, match.groups())) >= (0, 12, 3)
        )
        too_old = bool(match and not supported_version)
        unknown = "Compatibilité non confirmée. Vérifiez Ollama et OLLAMA_THINK_SUPPORT, puis redémarrez le backend."
        declared = metadata.get("capabilities") if metadata else None
        if isinstance(declared, list):
            if "completion" not in declared:
                return Capability(
                    False,
                    False,
                    reason="Ce modèle ne prend pas en charge la génération de réponses.",
                    version=version,
                )
            if "thinking" not in declared:
                return Capability(
                    True,
                    False,
                    reason="Le modèle ne déclare pas de capacité de raisonnement.",
                    version=version,
                )
            if config.ollama_think_support == "none":
                return Capability(
                    False,
                    False,
                    reason="Configuration contradictoire : le modèle déclare du raisonnement. Utilisez auto ou un modèle sans raisonnement.",
                    version=version,
                )
        details = metadata.get("details", {}) if metadata else {}
        info = metadata.get("model_info", {}) if metadata else {}
        if not isinstance(details, dict) or not isinstance(info, dict):
            return Capability(False, False, reason=unknown, version=version)
        family = details.get("family") or info.get("general.architecture")
        if family == "gptoss":
            return Capability(
                False,
                False,
                reason="Ce modèle utilise des niveaux de raisonnement; les modes booléens ne sont pas pris en charge.",
                version=version,
            )
        if config.ollama_think_support == "boolean":
            if too_old:
                return Capability(
                    False,
                    False,
                    reason="Ollama 0.12.3 minimum requis pour les modes avec think.",
                    version=version,
                )
            # Explicit operator assertion for missing/unrecognized metadata only.
            return Capability(True, True, True, version=version)
        if config.ollama_think_support == "none":
            return Capability(
                True,
                False,
                reason="Ce modèle est configuré sans raisonnement activable.",
                version=version,
            )
        if metadata is None:
            return Capability(False, False, reason=unknown, version=version)
        capabilities = metadata.get("capabilities")
        template = metadata.get("template", "")
        # Qwen3's template must actually branch on Think, unlike always-thinking models.
        toggles = isinstance(template, str) and bool(
            re.search(r"\bif\s+(?:not\s+)?\.Think\b", template)
        )
        if supported_version and family in {"qwen3", "qwen3moe"} and toggles:
            if capabilities is None or (
                isinstance(capabilities, list)
                and "thinking" in capabilities
                and "completion" in capabilities
            ):
                return Capability(True, True, True, version=version)
        return Capability(False, False, reason=unknown, version=version)

    async def require(self, mode):
        if mode not in {"fast", "reflection", "search"}:
            raise ResponseProblem("INVALID_MODE", "Mode de réponse inconnu.", 422)
        if mode == "search":
            return None
        capability = await self.capabilities()
        if not getattr(capability, mode):
            raise ResponseProblem("MODE_UNAVAILABLE", capability.reason, 422)
        return capability

    def budget(self, mode):
        return (
            self.config.ollama_reflection_num_predict
            if mode == "reflection"
            else self.config.ollama_num_predict
        )

    async def generate(self, question, passages, mode, capability):
        output_schema = GeneratedAnswer.model_json_schema()
        output_schema["$defs"]["Selection"]["properties"]["source_id"]["enum"] = [
            p.source_id for p in passages
        ]
        payload = {
            "model": self.config.ollama_model,
            "stream": False,
            "keep_alive": self.config.ollama_keep_alive_seconds,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": model_message(question, passages)},
            ],
            "options": {
                "temperature": 0,
                "num_ctx": self.config.ollama_num_ctx,
                "num_predict": self.budget(mode),
            },
        }
        if capability.boolean_think:
            payload["think"] = mode == "reflection"
        # 0.12.3 applies grammar before the thinking parser. Constraining all
        # tokens to JSON can prevent </think>. Validate the final JSON ourselves.
        if mode == "fast":
            payload["format"] = output_schema
        response = await self._http().post(
            f"{self.config.ollama_url}/api/chat", json=payload
        )
        if response.status_code == 400:
            self._capability = replace(
                self._capability or capability,
                **{mode: False},
                reason="Ollama a refusé ce mode ou ses réglages. Vérifiez la configuration puis redémarrez le backend.",
            )
            raise ResponseProblem(
                "MODEL_REJECTED_MODE",
                "Ollama refuse ce mode ou ses réglages. Vérifiez la configuration; aucun autre mode n’a été lancé.",
                422,
            )
        response.raise_for_status()
        try:
            envelope = response.json()
        except ValueError as exc:
            raise ResponseProblem(
                "INVALID_GENERATION",
                "Réponse Ollama illisible; aucune procédure affichée.",
                502,
            ) from exc
        if (
            not isinstance(envelope, dict)
            or envelope.get("done") is not True
            or envelope.get("done_reason") != "stop"
        ):
            raise ResponseProblem(
                "INCOMPLETE_GENERATION",
                "Génération interrompue ou budget atteint. Aucune procédure incomplète affichée.",
                502,
            )
        # Thinking never leaves this adapter, is never logged or used as evidence.
        message = envelope.get("message")
        if isinstance(message, dict):
            envelope["message"] = {k: v for k, v in message.items() if k != "thinking"}
        return envelope
