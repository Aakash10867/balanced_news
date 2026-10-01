"""Print the model IDs your Gemini key can use, and how config/models.yaml maps onto them.

    GEMINI_API_KEY=... python -m nishpaksh.tools.list_models
"""
from nishpaksh.config import gemini_api_key, load_yaml
from nishpaksh.router import GeminiBackend, Router


def main() -> None:
    backend = GeminiBackend(gemini_api_key())
    available = sorted(backend.list_models())
    print(f"{len(available)} models on this key:")
    for m in available:
        print("  ", m)
    router = Router(load_yaml("models.yaml")["tiers"], backend)
    router.resolve()
    print("\nTier mapping:")
    for tier, slots in router.tiers.items():
        for s in slots:
            print(f"  {tier:9s} {s.id:40s} {'DISABLED' if s.disabled else 'ok'}")


if __name__ == "__main__":
    main()
