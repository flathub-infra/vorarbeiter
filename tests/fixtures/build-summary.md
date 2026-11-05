# Flatpak summary fixture

`build-summary.base64` is the unmodified, base64-encoded `repo/summary`
produced by Flatpak 1.18.4 with:

```sh
mkdir -p build/files build/export
printf '[Application]\nname=org.test.App\nruntime=org.test.Platform/x86_64/1\nsdk=org.test.Sdk/x86_64/1\n' > build/metadata
printf 'size fixture payload\n' > build/files/payload
flatpak build-export --disable-sandbox --timestamp=2026-01-01 repo build test
base64 repo/summary
```

The app ref is `app/org.test.App/x86_64/test`. Its Flatpak sizes are 159
download bytes and 1024 installed bytes (two files rounded to 512-byte
blocks). Timestamps in a regenerated summary may differ.

The format and scope were checked against Flatpak's
`common/flatpak-repo-utils.c` (`generate_summary` and
`flatpak_repo_collect_extra_data_sizes`) and `common/flatpak-dir.c`
(`flatpak_remote_state_lookup_cache`):

- Summary v0 uses the double-variant-wrapped `xa.cache`, type `a{s(tts)}`.
- Summary v1 uses each ref's `xa.data`, type `(tts)`.
- Tuple order is installed bytes, download bytes, metadata; sizes are big endian.
- Summary generation includes declared extra-data sizes in those figures.

Synthetic GVariant fixtures cover distinct architectures, unrelated app and
runtime refs, missing size metadata, branch ambiguity, and summary v1.
