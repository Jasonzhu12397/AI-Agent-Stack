"""Report how model credentials resolve inside the console container.

An operator sees "The configured model credential is not available to the
runtime" with no clue which reference failed, so this prints every provider,
every account link and whether a member key is readable. Values are masked:
only the scheme and the variable name are shown, never the secret.
"""

import os
import sys

sys.path.insert(0, "/workspaces/AI-Agent-Stack/apps/platform-api")
sys.path.insert(0, "/workspaces/AI-Agent-Stack")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "platform_api.settings")
# The console runs on a sqlite file under .devdata; without this the probe
# would try the default Postgres settings and report a connection error.
os.environ.setdefault(
    "DATABASE_URL", "sqlite:////workspaces/AI-Agent-Stack/.devdata/console.db"
)
os.environ.setdefault("SECRET_KEY", "codespace-demo-secret-key-not-for-production-use")

import django  # noqa: E402

django.setup()

from core import provider_accounts  # noqa: E402
from core.models import ModelProvider  # noqa: E402
from core.provider_accounts import MemberCredentialSecret, ProviderAccountLink  # noqa: E402


def describe(reference):
    if not reference:
        return "<empty>"
    if reference.startswith("env:"):
        variable = reference.removeprefix("env:")
        return f"env:{variable} (set={bool(os.getenv(variable))})"
    if reference.startswith("member://"):
        return "member://<sealed>"
    return f"raw/<no-scheme> len={len(reference)}"


print("== providers ==")
for provider in ModelProvider.objects.select_related("tenant").order_by("name"):
    print(
        f"- {provider.name} | auth={provider.auth_mode} | preset={provider.preset_key or '-'} "
        f"| ref={describe(provider.credential_ref)} | enabled={provider.enabled} "
        f"| models={provider.models.count()}"
    )

print("== account links ==")
for link in ProviderAccountLink.objects.select_related("user", "provider").order_by("-linked_at"):
    readable = provider_accounts.resolve_member_secret(link.credential_ref, link.user)
    print(
        f"- {link.user.username} @ {link.provider.name} | status={link.status} "
        f"| ref={describe(link.credential_ref)} | readable={readable is not None}"
    )

print("== sealed member secrets ==", MemberCredentialSecret.objects.count())
print("== env keys present ==")
for name in sorted(os.environ):
    if name.startswith(("OPENAI", "DEEPSEEK", "QWEN", "ZHIPU", "MOONSHOT", "MINIMAX", "GOOGLE")):
        print(f"- {name}=<set:{len(os.environ[name])} chars>")
