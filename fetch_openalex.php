<?php
declare(strict_types=1);

error_reporting(E_ALL);
ini_set('display_errors', '1');
set_time_limit(0);

// ─────────────────────────────────────────────
// CONFIG
// ─────────────────────────────────────────────

$ROR_ID     = "05ect4e57";
$START_YEAR = 2020;
$MAILTO     = "lsulibrary@lsu.edu";

$DB_FILE   = "/home/lsuopena/tmp/openalex.sqlite";
$META_FILE = "/home/lsuopena/tmp/cache_metadata.json";
$LOG_FILE  = __DIR__ . "/fetch_log.txt";

$BASE_URL  = "https://api.openalex.org/works";

// ─────────────────────────────────────────────
// LOGGING
// ─────────────────────────────────────────────

function log_message(string $msg): void {
    global $LOG_FILE;

    $ts = date('Y-m-d H:i:s');
    $line = "[$ts] $msg\n";

    echo $line;
    file_put_contents($LOG_FILE, $line, FILE_APPEND);
}

// ─────────────────────────────────────────────
// DATABASE SETUP
// ─────────────────────────────────────────────

$db = new PDO("sqlite:$DB_FILE");

$db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);

$db->exec("PRAGMA journal_mode = WAL");
$db->exec("PRAGMA synchronous = NORMAL");

$db->exec("
CREATE TABLE IF NOT EXISTS works (
    id TEXT PRIMARY KEY,
    title TEXT,
    publication_year INTEGER,
    publication_date TEXT,
    cited_by_count INTEGER,
    type TEXT,
    doi TEXT,
    oa_status TEXT,
    is_oa INTEGER,
    apc_value REAL,
    apc_currency TEXT,
    journal TEXT,
    publisher TEXT,
    authors TEXT,
    funders TEXT,
    primary_topic TEXT
)");

$db->exec("
CREATE INDEX IF NOT EXISTS idx_year
ON works(publication_year)
");

$db->exec("
CREATE INDEX IF NOT EXISTS idx_cited
ON works(cited_by_count)
");

$db->exec("
CREATE INDEX IF NOT EXISTS idx_oa
ON works(is_oa)
");

// ─────────────────────────────────────────────
// PREPARED INSERT
// ─────────────────────────────────────────────

$insert = $db->prepare("
INSERT OR REPLACE INTO works (
    id,
    title,
    publication_year,
    publication_date,
    cited_by_count,
    type,
    doi,
    oa_status,
    is_oa,
    apc_value,
    apc_currency,
    journal,
    publisher,
    authors,
    funders,
    primary_topic
)
VALUES (
    :id,
    :title,
    :year,
    :date,
    :cited,
    :type,
    :doi,
    :oa_status,
    :is_oa,
    :apc_value,
    :apc_currency,
    :journal,
    :publisher,
    :authors,
    :funders,
    :topic
)
");

// ─────────────────────────────────────────────
// FETCH LOOP
// ─────────────────────────────────────────────

log_message("========================================");
log_message("=== Starting OpenAlex fetch ===");
log_message("========================================");

$cursor = "*";
$total = 0;
$firstPage = true;

$today = date('Y-m-d');

log_message("Date: $today");
log_message("Fetching works from $START_YEAR through today");

// ─────────────────────────────────────────────
// MAIN LOOP
// ─────────────────────────────────────────────

while (true) {

    $filter =
        "institutions.ror:$ROR_ID," .
        "from_publication_date:$START_YEAR-01-01," .
        "to_publication_date:$today";

    $query = http_build_query([
        'filter'   => $filter,
        'per-page' => 100,
        'cursor'   => $cursor,
        'select'   =>
            "id,title,publication_year,publication_date,cited_by_count," .
            "primary_location,open_access,type,doi,apc_list," .
            "authorships,primary_topic,funders",
        'mailto'   => $MAILTO
    ], '', '&', PHP_QUERY_RFC3986);

    $url = "$BASE_URL?$query";

    log_message("Requesting OpenAlex...");
    log_message("Cursor: " . substr($cursor, 0, 80));

    // ─────────────────────────────────────────
    // CURL REQUEST
    // ─────────────────────────────────────────

    $ch = curl_init($url);

    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_CONNECTTIMEOUT => 30,
        CURLOPT_TIMEOUT        => 120,
        CURLOPT_USERAGENT      => 'LSU OpenAlex Research Dashboard/1.0',
        CURLOPT_HTTPHEADER     => [
            'Accept: application/json'
        ]
    ]);

    $json = curl_exec($ch);

    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $curlError = curl_error($ch);

    curl_close($ch);

    // ─────────────────────────────────────────
    // CURL ERROR
    // ─────────────────────────────────────────

    if ($json === false || $json === '') {

        log_message("ERROR: Empty response from OpenAlex.");

        if ($curlError) {
            log_message("cURL error: $curlError");
        }

        log_message("HTTP status: $httpCode");

        break;
    }

    // ─────────────────────────────────────────
    // HTTP ERROR
    // ─────────────────────────────────────────

    if ($httpCode < 200 || $httpCode >= 300) {

        log_message("ERROR: OpenAlex returned HTTP $httpCode");

        // Save the actual response from OpenAlex
        log_message("OpenAlex response:");
        log_message(substr($json, 0, 2000));

        break;
    }

    // ─────────────────────────────────────────
    // JSON DECODE
    // ─────────────────────────────────────────

    $data = json_decode($json, true);

    if (!is_array($data)) {

        log_message("ERROR: Invalid JSON returned by OpenAlex.");
        log_message(substr($json, 0, 2000));

        break;
    }

    // ─────────────────────────────────────────
    // API ERROR
    // ─────────────────────────────────────────

    if (isset($data['error'])) {

        log_message("ERROR: OpenAlex API error.");
        log_message(json_encode($data));

        break;
    }

    // ─────────────────────────────────────────
    // RESULTS
    // ─────────────────────────────────────────

    $results = $data['results'] ?? [];

    if (!is_array($results)) {
        log_message("ERROR: Results field is not an array.");
        break;
    }

    $resultCount = count($results);

    log_message("Received $resultCount works.");

    // ─────────────────────────────────────────
    // ONLY CLEAR DATABASE AFTER SUCCESSFUL
    // FIRST PAGE
    // ─────────────────────────────────────────

    if ($firstPage) {

        log_message("First page successfully received.");
        log_message("Clearing existing records...");

        $db->exec("DELETE FROM works");

        $firstPage = false;
    }

    // ─────────────────────────────────────────
    // INSERT RESULTS
    // ─────────────────────────────────────────

    if ($resultCount > 0) {

        $db->beginTransaction();

        try {

            foreach ($results as $w) {

                // ─────────────────────────────
                // AUTHORS
                // ─────────────────────────────

                $authors = [];

                foreach ($w['authorships'] ?? [] as $a) {

                    foreach ($a['institutions'] ?? [] as $i) {

                        if (
                            !empty($i['ror']) &&
                            str_ends_with($i['ror'], $ROR_ID)
                        ) {

                            $authorName =
                                $a['author']['display_name'] ?? null;

                            if ($authorName) {
                                $authors[] = $authorName;
                            }

                            break;
                        }
                    }
                }

                // ─────────────────────────────
                // FUNDERS
                // ─────────────────────────────

                $funders = [];

                foreach ($w['funders'] ?? [] as $f) {

                    if (!empty($f['display_name'])) {
                        $funders[] = $f['display_name'];
                    }
                }

                // ─────────────────────────────
                // INSERT
                // ─────────────────────────────

                $insert->execute([

                    ':id' =>
                        $w['id'] ?? '',

                    ':title' =>
                        $w['title'] ?? '',

                    ':year' =>
                        $w['publication_year'] ?? null,

                    ':date' =>
                        $w['publication_date'] ?? '',

                    ':cited' =>
                        $w['cited_by_count'] ?? 0,

                    ':type' =>
                        $w['type'] ?? '',

                    ':doi' =>
                        $w['doi'] ?? '',

                    ':oa_status' =>
                        $w['open_access']['oa_status'] ?? '',

                    ':is_oa' =>
                        !empty($w['open_access']['is_oa'])
                            ? 1
                            : 0,

                    ':apc_value' =>
                        $w['apc_list']['value_usd'] ?? null,

                    ':apc_currency' =>
                        $w['apc_list']['currency'] ?? null,

                    ':journal' =>
                        $w['primary_location']['source']['display_name']
                        ?? '',

                    ':publisher' =>
                        $w['primary_location']['source']['host_organization_name']
                        ?? '',

                    ':authors' =>
                        json_encode(
                            array_values(
                                array_unique($authors)
                            ),
                            JSON_UNESCAPED_UNICODE
                        ),

                    ':funders' =>
                        json_encode(
                            array_values(
                                array_unique($funders)
                            ),
                            JSON_UNESCAPED_UNICODE
                        ),

                    ':topic' =>
                        $w['primary_topic']['display_name'] ?? ''
                ]);

                $total++;
            }

            $db->commit();

        } catch (Throwable $e) {

            if ($db->inTransaction()) {
                $db->rollBack();
            }

            log_message(
                "DATABASE ERROR: " . $e->getMessage()
            );

            break;
        }
    }

    log_message("Total inserted so far: $total");

    // ─────────────────────────────────────────
    // NEXT CURSOR
    // ─────────────────────────────────────────

    $nextCursor =
        $data['meta']['next_cursor'] ?? null;

    if (!$nextCursor) {

        log_message("No next cursor. Fetch complete.");
        break;
    }

    if ($nextCursor === $cursor) {

        log_message(
            "WARNING: Cursor did not change. Stopping to prevent loop."
        );

        break;
    }

    $cursor = $nextCursor;

    // Small pause between requests
    usleep(150000);
}

// ─────────────────────────────────────────────
// METADATA
// ─────────────────────────────────────────────

$success = !$firstPage && $total > 0;

file_put_contents(
    $META_FILE,
    json_encode(
        [
            'last_updated' => date('c'),
            'total_works'  => $total,
            'status'       => $success ? 'success' : 'failed'
        ],
        JSON_PRETTY_PRINT
    )
);

log_message("========================================");

if ($success) {
    log_message("=== Fetch completed successfully ===");
} else {
    log_message("=== Fetch FAILED ===");
}

log_message("Total works inserted: $total");

if (file_exists($DB_FILE)) {
    log_message(
        "SQLite size: " .
        filesize($DB_FILE) .
        " bytes"
    );
}

log_message("========================================");