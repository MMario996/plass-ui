# Phrase Platform APIs – Postman-Collection

Request-Sammlung und ausführbare Test-Projekte für fünf Phrase-APIs, generiert aus den OpenAPI-Specs in `specs/`.

| Datei | Inhalt |
|---|---|
| `Phrase-Platform-APIs.postman_collection.json` | Collection (Postman v2.1) |
| `Phrase-EU.postman_environment.json` / `Phrase-US.postman_environment.json` | Base-URLs und Testparameter je Rechenzentrum |
| `sample-files/` | Upload-Dateien (Style Guides in Markdown, Text-, Audio-Datei) |
| `specs/` | OpenAPI-Quellen |
| `generate_collection.py` | Generator: `python3 generate_collection.py` baut Collection und Environments neu |

## Einrichtung

1. Collection und eines der Environments in Postman importieren, dann das Environment auswählen.
2. Im Environment setzen:
   - `accessToken`: IDM-JWT für Control Hub, Style Guides, Quality Evaluator und Connectors
   - `studioApiKey`: Studio API-Key (wird als `X-API-Key` gesendet)
   - optional `webhookUrl` (z. B. von webhook.site) für die Async-Endpunkte
3. Unter *Settings → General → Working Directory* den Ordner `postman/phrase` eintragen. Die Datei-Uploads verweisen auf `sample-files/…`.

## Aufbau

### 📚 API-Referenz (116 Requests)

| API | Requests | Ordner |
|---|---|---|
| Control Hub (Content Groups) | 10 | Groups, Groups references |
| Style Guides & Rules | 20 | Style Guide, Rule |
| Quality Evaluator | 3 | Analytics, Evaluation, Quality Profiles |
| Studio | 16 | Projects, Recordings, Subtitle Profiles, Pronunciations, Glossaries, Insights, Safe Communications, Subtitles |
| Connectors (Bifrost) | 67 | Files, GitHub, Braze, Contentful, Google Drive, Connectors, Tridion Docs, Optimizely, jeweils mit Sync/Async |

- Pfadparameter sind an Collection-Variablen gebunden, z. B. `:groupId` an `{{groupId}}` und `:id` an `{{styleGuideId}}` bzw. `{{ruleId}}`.
- Pflicht-Query-Parameter sind aktiv. Optionale Parameter sind vorbelegt, aber deaktiviert.
- Bodies stammen aus den Spec-Beispielen. Connector-UUIDs, `storageId` und `contentGroupId` sind durch Variablen ersetzt.
- Beispiel-Responses aus der Spec sind als *Examples* hinterlegt.
- Create-Requests speichern die erzeugten IDs automatisch, z. B. `groupId`, `ruleId`, `createJobId` und `projectId`.

### 🧪 Test-Szenarien (185 Requests in 15 Projekten)

Jedes Projekt legt seine eigenen Testdaten an und löscht sie am Ende wieder. Asynchrone Jobs werden gepollt (`pollIntervalMs`, `pollMaxAttempts`).

| # | Projekt | Was getestet wird |
|---|---|---|
| 00 | Smoke-Test | Jede API einmal ohne und einmal mit Credentials |
| 01 | Control Hub – Lifecycle | CRUD für Content Groups, Referenzen verknüpfen, suchen und entfernen, Reverse Lookup, 404 nach dem Löschen |
| 02 | Control Hub – Negativtests | 400/401/403/404/422, SYSTEM-Gruppe schreibgeschützt, ein TMS-Projekt nur in einer Gruppe |
| 03 | Style Guides – Lifecycle | Markdown-Upload (v2, async), Job-Polling, Versionen, Datei-Download, extrahierte Rules, Update nur der Metadaten (200) und mit Datei (202), Duplikatregel, Cleanup |
| 04 | Rules – CRUD, Bulk & Export | Anlegen, Suchen, PATCH, Aktivieren und Deaktivieren einzeln und per Bulk, Markdown-Export, Einzel- und Bulk-Löschung |
| 05 | Rules & Style Guides – Negativtests | Kombinationen von `active`/`aiCheckEnabled` und `allContentGroups`/`allLanguages`, Längen- und Paging-Limits, Upload-Validierung |
| 06 | Quality Evaluator – E2E | AI-Check-Rule anlegen, Eligibility abwarten, Quality Profile, Evaluation (formell, informell, leer), 400-Fälle, Analytics |
| 07 | Cross-API Governance-Flow | Control Hub → Style Guide → extrahierte Rules → Quality Profile → Evaluation, Unlink-Schutz (409) |
| 08 | Studio – Ressourcen | Stammdaten, Projektlisten mit Filtern und Paging, Fehlerfälle |
| 09 | Studio – Projekt-Pipeline | Projekt aus URL, Transkription (SRT/VTT), Untertitel-Round-Trip, Übersetzung, Dubbing, PATCH, Löschen |
| 10 | Connectors – Discovery & Files | Connector-UUIDs automatisch übernehmen, File Storage hoch- und herunterladen mit SHA-256-Vergleich |
| 11 | Contentful Roundtrip | Listing, Raw- und XLIFF-Download, Konvertierung in beide Richtungen, XLIFF-Upload, Async mit Webhook |
| 12 | Google Drive | Download, Upload aus dem Storage, zweiphasiger Streaming-Upload |
| 13 | GitHub | Branch-Listing (Tree API), Download, Batch-Upload als PR |
| 14 | Braze / Optimizely / Tridion | Listing als Smoke-Test |

**Hinweise**

- Schreibende Connector-Tests laufen nur mit `connectorAllowWrite = true`.
- Requests, denen eine Konfigurationsvariable fehlt (z. B. `contentfulSpaceId`), werden automatisch übersprungen.
- Tests mit dem Präfix **[LLM]** hängen von der Modellentscheidung ab und können schwanken.
- Evaluation und Analytics im Quality Evaluator brauchen die IDM-Rolle ADMIN oder OWNER.
- Szenario 09 verbraucht Studio-Kontingent. Die Testdatei wird über `studioSampleMediaUrl` festgelegt.
- Polling über `setNextRequest` funktioniert nur im Collection Runner bzw. in Newman, nicht bei Einzel-Requests.
- Für Control Hub nennt die Spec nur EU. Die US-URL (`https://us.phrase.com/control-hub`) ist analog angenommen.

## Newman (CLI / CI)

```bash
npx newman run postman/phrase/Phrase-Platform-APIs.postman_collection.json \
  -e postman/phrase/Phrase-EU.postman_environment.json \
  --env-var accessToken="$PHRASE_TOKEN" --env-var studioApiKey="$STUDIO_KEY" \
  --working-dir postman/phrase \
  --folder "00 · Smoke-Test – Erreichbarkeit & Authentifizierung" \
  --folder "01 · Control Hub – Content-Group-Lifecycle"
```

Ohne `--folder` läuft die ganze Collection inklusive der API-Referenz. Für Testläufe deshalb einzelne Projekte über `--folder` auswählen.
