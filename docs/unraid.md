# MusicOps on Unraid

The Docker Hub repository is `ziyaolin/musicops`. Use the registry digest from a verified release as the Repository field in Unraid. The template's `latest` reference is for discovery. Record the specific digest and source commit for every production change.

The first published build is `ziyaolin/musicops:sha-671f59811a98`.
Its pinned reference is `ziyaolin/musicops@sha256:9491265268978dbfbda71d72a3dcebf47e4ff7f5829541367dd8b804e67d0978`.

## Prepare the container

- Use `templates/musicops-unraid.xml` as the Unraid user template and manage the container on the Docker tab. For a migration, retain the `musicops_default` network and host port 8086. For a fresh install, select an existing appropriate bridge network or create `musicops_default` before applying the template.
- Keep `/data`, `/music`, and `/sessions` on their existing host paths when migrating. The API token is mounted read-only at `/run/secrets/musicops_api_token`.
- Provision `/mnt/user/appdata/musicops/secrets/unraid-runtime.env` with owner-only permissions. Use Docker `--env-file` syntax and copy only the application settings needed by this instance, such as `MUSICOPS_PUBLIC_BASE_URL`, metadata provider configuration, mutation mode, and configured Telegram, proxy, Navidrome, or STT settings. Preserve the actual production values locally. Omit legacy translation settings, Compose image selectors, API token values, and build metadata. The API token file and image labels are managed separately from this file.
- Confirm the generated configuration preserves TCP `8086:8088`, the desired custom network, the bind mount modes, and the capability and restart settings from the template.

## Verify and cut over

1. Smoke-test the pinned image with temporary data and library directories and a synthetic token. Verify `/health/ready`, image provenance, and the authenticated `/actions` contract.
2. Verify storage health and the previous image, configuration, and SQLite backup. For a Compose migration, preserve the Compose configuration and avoid `compose down`, which can remove the custom network. Retain the previous image locally. Free the production name and port before applying the Unraid template.
3. After applying, verify the actual running image ID against the digest, local and public readiness, authenticated Catalog, persistent mounts, and the worker. Confirm `lyrics.translate` is absent and both library write schemas accept the Agent translation payload. Use isolated resources or read-only checks for verification.
4. If the new container fails, stop it and restore the previous image and configuration. For the initial Compose migration, use the saved Compose configuration and previous local image. Confirm readiness and the Catalog after restoration. Restore the database snapshot only when recovery requires it, after stopping writes and preserving the current database.
