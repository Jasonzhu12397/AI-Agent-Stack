# Vendored patches

Intentional divergences from upstream `metal3-io/metal3-dev-env` at commit
`7076f07624cdfba990f83bea24f749b37e3baad7`. Each entry records the file, the
reason, and how to verify the change. Local edits are allowed and expected; this
file keeps them reviewable and re-appliable after `run.sh restore`.

Format:

```
## <file>
- Reason: <why this repository must diverge>
- Change: <what changed, one sentence>
- Check: python3 tools/vendor_import.py --check   # expect one `local change:` line for this file
```

## Entries

_None yet. The imported tree is verbatim upstream source._
