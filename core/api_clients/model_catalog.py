"""Model discovery for OpenAI-compatible endpoints."""

from urllib.parse import urlsplit

from openai import APIConnectionError, APITimeoutError, AuthenticationError, OpenAI, OpenAIError


def fetch_openai_models(base_url, api_key):
    """Return model IDs without issuing a completion or retrying requests."""
    base_url, api_key = base_url.strip(), api_key.strip()
    try:
        parsed = urlsplit(base_url)
        valid_url = parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("API 地址必须是有效的 HTTP 或 HTTPS 基础地址。")
    if not api_key:
        raise ValueError("API Key 不能为空。")

    try:
        with OpenAI(base_url=base_url, api_key=api_key, timeout=15.0, max_retries=0) as client:
            response = client.models.list()
            data = getattr(response, "data", None)
            if not isinstance(data, list):
                raise ValueError("接口未返回有效的模型列表。")
            model_ids = {
                model.id.strip() for model in data
                if isinstance(getattr(model, "id", None), str) and model.id.strip()
            }
            if data and not model_ids:
                raise ValueError("模型列表中没有有效的模型 ID。")
            return sorted(model_ids, key=lambda value: (value.casefold(), value))
    except AuthenticationError:
        raise ConnectionError("API 认证失败，请检查 Key。") from None
    except APITimeoutError:
        raise ConnectionError("获取模型列表超时。") from None
    except APIConnectionError:
        raise ConnectionError("无法连接到 API 服务器。") from None
    except OpenAIError as error:
        status = getattr(error, "status_code", None)
        if status in (404, 405):
            message = "接口不支持模型列表，或 API 基础地址不正确。"
        elif status == 403:
            message = "API Key 无权访问模型列表。"
        elif status == 429:
            message = "模型列表请求受限，请稍后刷新。"
        else:
            message = f"模型列表请求失败 (HTTP {status})。" if status else "接口返回的模型列表无效。"
        # Gateway error bodies can echo credentials; only expose known messages.
        raise ConnectionError(message) from None
