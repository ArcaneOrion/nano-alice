from nano_alice.providers.custom_provider import CustomProvider


def test_custom_provider_normalizes_prefixed_default_model():
    provider = CustomProvider(default_model="openai/gpt-5.4")

    assert provider.default_model == "gpt-5.4"


def test_custom_provider_normalizes_prefixed_runtime_model():
    provider = CustomProvider(default_model="gpt-5.4")

    assert provider._normalize_model_name("openai/gpt-5.4") == "gpt-5.4"


def test_custom_provider_normalizes_internal_openai_route_default_model():
    provider = CustomProvider(default_model="openai1/gpt-5.4")

    assert provider.default_model == "gpt-5.4"


def test_custom_provider_normalizes_internal_openai_route_runtime_model():
    provider = CustomProvider(default_model="gpt-5.4")

    assert provider._normalize_model_name("openai1/gpt-5.4") == "gpt-5.4"
    assert provider._normalize_model_name("openai_1/gpt-5.4") == "gpt-5.4"
    assert provider._normalize_model_name("openai-1/gpt-5.4") == "gpt-5.4"


def test_custom_provider_preserves_non_openai_route_prefixes():
    provider = CustomProvider(default_model="gpt-5.4")

    assert provider._normalize_model_name("minimax/MiniMax-M2.5") == "minimax/MiniMax-M2.5"
