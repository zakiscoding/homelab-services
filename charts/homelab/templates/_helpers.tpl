{{/*
Resource names are the bare component names (api, postgres, prometheus, ...) so in-cluster DNS
matches docker-compose's service names. That keeps the copied prometheus.yml targets
("api:8000") and the Grafana datasource ("http://prometheus:9090") valid without edits.
Consequence: install one release per namespace.
*/}}

{{- define "homelab.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common labels. Call with (dict "ctx" $ "component" "api") */}}
{{- define "homelab.labels" -}}
helm.sh/chart: {{ include "homelab.chart" .ctx }}
app.kubernetes.io/name: {{ .component }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/part-of: homelab
app.kubernetes.io/version: {{ .ctx.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .ctx.Release.Service }}
{{- end -}}

{{- define "homelab.selectorLabels" -}}
app.kubernetes.io/name: {{ .component }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
{{- end -}}

{{/* Argo CD sync wave annotation. Call with (dict "ctx" $ "component" "api") */}}
{{- define "homelab.syncWave" -}}
argocd.argoproj.io/sync-wave: {{ index .ctx.Values.syncWaves .component | default 0 | toString | quote }}
{{- end -}}

{{/* image: "repo:tag". Call with the component's .image */}}
{{- define "homelab.image" -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}

{{- define "homelab.secretName" -}}
{{- required "secrets.existingSecret is required: create the Secret out of band (see charts/README.md)" .Values.secrets.existingSecret -}}
{{- end -}}

{{/* env var from the existing Secret. Call with (dict "ctx" $ "name" "API_KEY" "optional" false) */}}
{{- define "homelab.secretEnv" -}}
- name: {{ .name }}
  valueFrom:
    secretKeyRef:
      name: {{ include "homelab.secretName" .ctx }}
      key: {{ .name }}
      {{- if .optional }}
      optional: true
      {{- end }}
{{- end -}}

{{/* POSTGRES_USER/PASSWORD/DB from the Secret, then DATABASE_URL built with $(VAR) expansion */}}
{{- define "homelab.databaseEnv" -}}
{{ include "homelab.secretEnv" (dict "ctx" . "name" "POSTGRES_USER") }}
{{ include "homelab.secretEnv" (dict "ctx" . "name" "POSTGRES_PASSWORD") }}
{{ include "homelab.secretEnv" (dict "ctx" . "name" "POSTGRES_DB") }}
- name: DATABASE_URL
  value: "postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@postgres:5432/$(POSTGRES_DB)"
{{- end -}}

{{/* initContainers block that waits until Postgres accepts connections (compose:
depends_on postgres service_healthy). Renders nothing when postgres or the wait is disabled. */}}
{{- define "homelab.waitForPostgres" -}}
{{- if and .Values.postgres.enabled .Values.waitForPostgres.enabled -}}
initContainers:
  - name: wait-for-postgres
    image: {{ include "homelab.image" .Values.postgres.image }}
    imagePullPolicy: {{ .Values.postgres.image.pullPolicy }}
    command: ["sh", "-c", "until pg_isready -h postgres -p 5432 -t 2; do echo waiting for postgres; sleep 2; done"]
    securityContext:
      {{- toYaml .Values.containerSecurityContext | nindent 6 }}
    resources:
      {{- toYaml .Values.waitForPostgres.resources | nindent 6 }}
{{- end -}}
{{- end -}}

{{/* Pod-level settings shared by every workload. Call with (dict "ctx" $ "comp" .Values.api) */}}
{{- define "homelab.podCommon" -}}
automountServiceAccountToken: false
{{- with .ctx.Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
securityContext:
  {{- toYaml (.comp.podSecurityContext | default .ctx.Values.podSecurityContext) | nindent 2 }}
{{- with .comp.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .comp.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* volumeClaimTemplates entry or nothing. Call with (dict "ctx" $ "comp" .Values.postgres "name" "data") */}}
{{- define "homelab.volumeClaimTemplate" -}}
{{- if .comp.persistence.enabled }}
volumeClaimTemplates:
  - metadata:
      name: {{ .name }}
      labels:
        app.kubernetes.io/part-of: homelab
    spec:
      accessModes: [ReadWriteOnce]
      {{- $sc := .comp.persistence.storageClass | default .ctx.Values.storageClass }}
      {{- if $sc }}
      storageClassName: {{ $sc | quote }}
      {{- end }}
      resources:
        requests:
          storage: {{ .comp.persistence.size }}
{{- end }}
{{- end -}}

{{/* Matching pod volume when persistence is off */}}
{{- define "homelab.emptyDirIfNoPersistence" -}}
{{- if not .comp.persistence.enabled }}
- name: {{ .name }}
  emptyDir: {}
{{- end }}
{{- end -}}
