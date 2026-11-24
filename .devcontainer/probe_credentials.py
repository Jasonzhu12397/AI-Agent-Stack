# ruff: noqa: E402
"""Report how model credentials resolve inside the console container.

An operator sees "The configured model credential is not available to the
runtime" with no clue which reference failed, so this prints every provider,
every account link and whether a member key is readable. Values are masked:
only the scheme and the variable name are shown, never the secret.
"""

import os
import sys
from pathlib import Path

#: Resolve the checkout from this file, so the probe reads the same database
#: the console writes to even if the codespace folder name differs.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "platform-api"))
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "platform_api.settings")
# The console runs on a sqlite file under .devdata; without this the probe
# would try the default Postgres settings and report a connection error.
# The console writes to this file; a login shell may export a different value,
# so the probe sets it outright instead of inheriting one.
os.environ["DATABASE_URL"] = f"sqlite:///{REPO_ROOT / '.devdata' / 'console.db'}"
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


from django.conf import settings as django_settings  # noqa: E402

print(f"== db == {django_settings.DATABASES['default'].get('NAME')}")
from core.models import ModelConfig as _Config  # noqa: E402
from core.models import Tenant as _Tenant
from django.db import connection  # noqa: E402

with connection.cursor() as cursor:
    cursor.execute("select count(*) from core_modelprovider")
    rows = cursor.fetchone()[0]
print(f"== raw provider rows == {rows}")
print(f"== tenants == {list(_Tenant.objects.values_list('slug', flat=True))}")
print(f"== model configs == {_Config.objects.count()}")
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
