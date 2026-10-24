from __future__ import annotations

import re


def public_error(error: Exception) -> dict[str, str]:
    """Keep useful domain errors, but never expose raw SDK exceptions/URLs."""
    current: BaseException | None = error
    for _ in range(6):
        if current is None:
            break
        status = getattr(current, "status_code", None)
        name = type(current).__name__.lower()
        if status in {401, 403}:
            return {
                "code": "provider_auth",
                "detail": "模型或数据服务鉴权失败，请检查本地 Key、接口地址及访问权限；原行程已保留。",
            }
        if status == 429:
            return {
                "code": "provider_rate_limit",
                "detail": "模型或数据服务触发限流，请稍后重试；原行程已保留。",
            }
        if status in {408, 504} or "timeout" in name:
            return {
                "code": "provider_timeout",
                "detail": "模型或数据服务响应超时，请稍后重试；原行程已保留。",
            }
        current = current.__cause__ or current.__context__
    if isinstance(error, ValueError):
        message = re.sub(r"https?://\S+", "[服务地址已隐藏]", str(error))
        message = re.sub(
            r"(?i)(?:api[_-]?key|authorization|access[_-]?token|bearer|key)\s*[:= ]\s*[^\s,;，；]+",
            "[凭证已隐藏]",
            message,
        )
        return {
            "code": "invalid_plan",
            "detail": message[:1000] or "当前约束无法生成可执行行程，请调整需求。",
        }
    return {
        "code": "internal_error",
        "detail": "规划服务暂时无法完成请求，请稍后重试；原行程已保留。",
    }
