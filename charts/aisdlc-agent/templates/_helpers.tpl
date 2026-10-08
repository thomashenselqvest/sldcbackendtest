{{- define "agent.name" -}}{{ .Values.name | default .Release.Name }}{{- end -}}
{{- define "agent.labels" -}}
aisdlc.io/agent: {{ include "agent.name" . }}
app.kubernetes.io/part-of: aisdlc-agents
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
