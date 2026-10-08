# Local patches to the vendored Turnstone chart

Source: https://github.com/turnstonelabs/turnstone, tag `v1.8.5`, path `deploy/helm/turnstone`
(Bitnami postgresql 18.8.17 vendored in `charts/` via `helm dependency build`).

1. `templates/job-migrate.yaml` is wrapped in `{{- if .Values.migrate.enabled }}` (default `true`).
   Under Argo CD the Job's `post-install,pre-upgrade` hook becomes a PreSync hook that runs
   before Postgres exists and deadlocks the first sync. Our values set `migrate.enabled: false`;
   server, console and `turnstone-admin` run migrations on startup (`init_storage(run_migrations=True)`).
2. `Chart.yaml`: `appVersion` 0.3.0 -> 1.8.5 (upstream never bumped it), chart `version` suffixed `-sdlc.1`.
3. `templates/deployment-server.yaml` + `values.yaml`: generic `server.extraInitContainers`,
   `server.extraEnv`, `server.extraVolumeMounts`, `server.extraVolumes` (all empty by default).
   Used to install the platform-admin CLIs (kubectl, argo, argocd, helm) into the server pod.
