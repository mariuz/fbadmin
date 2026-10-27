{{- define "fbadmin.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "fbadmin.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "fbadmin.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "fbadmin.labels" -}}
helm.sh/chart: {{ include "fbadmin.chart" . }}
{{ include "fbadmin.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "fbadmin.selectorLabels" -}}
app.kubernetes.io/name: {{ include "fbadmin.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "fbadmin.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "fbadmin.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "fbadmin.secretName" -}}
{{- default (printf "%s-credentials" (include "fbadmin.fullname" .)) .Values.serverMode.existingSecret }}
{{- end }}

{{- define "fbadmin.claimName" -}}
{{- default (printf "%s-data" (include "fbadmin.fullname" .)) .Values.persistence.existingClaim }}
{{- end }}

{{/*
RBAC rules of the Kubernetes page (docs/KUBERNETES.md).
*/}}
{{- define "fbadmin.kubernetesRules" -}}
- apiGroups: [firebird.cloudnative-firebird.io]
  resources: [firebirdclusters]
  verbs: {{ ternary "[get, list, patch]" "[get, list]" .Values.kubernetes.manage }}
- apiGroups: [firebird.cloudnative-firebird.io]
  resources: [firebirdbackups, firebirdrestores]
  verbs: {{ ternary "[get, list, create]" "[get, list]" .Values.kubernetes.manage }}
- apiGroups: [firebird.cloudnative-firebird.io]
  resources: [firebirdscheduledbackups, firebirddatabases, firebirdusers]
  verbs: [get, list]
- apiGroups: [""]
  resources: [pods]
  verbs: {{ ternary "[get, list, delete]" "[get, list]" .Values.kubernetes.manage }}
- apiGroups: [""]
  resources: [pods/log, events]
  verbs: [get, list]
{{- if .Values.kubernetes.manage }}
- apiGroups: [apps]
  resources: [statefulsets]
  verbs: [get, patch]
{{- end }}
{{- if .Values.kubernetes.readSecrets }}
- apiGroups: [""]
  resources: [secrets]
  verbs: [get]
{{- end }}
{{- end }}
