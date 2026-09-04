<?php
declare(strict_types=1);

/**
 * works_api.php
 * Streaming JSON API for OpenAlex cached SQLite data
 * (NO raw_json, quota-safe, memory-safe)
 */

header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: *');

ini_set('display_errors', '0');
ini_set('log_errors', '1');
error_reporting(E_ALL);

ob_start();

// ─────────────────────────────────────────────
// ERROR HANDLING
// ─────────────────────────────────────────────

set_exception_handler(function (Throwable $e) {
  http_response_code(500);
  ob_clean();
  echo json_encode([
    'error'   => 'Unhandled exception',
    'message' => $e->getMessage()
  ]);
  exit;
});

set_error_handler(function ($severity, $message, $file, $line) {
  throw new ErrorException($message, 0, $severity, $file, $line);
});

// ─────────────────────────────────────────────
// DB CONNECTION
// ─────────────────────────────────────────────

$DB_FILE   = "/home/lsuopena/tmp/openalex.sqlite";
$META_FILE = "/home/lsuopena/tmp/cache_metadata.json";

if (!file_exists($DB_FILE)) {
  echo json_encode([
    'error' => 'Database not found. Run fetch_openalex.php first.'
  ]);
  exit;
}

$db = new PDO("sqlite:$DB_FILE", null, null, [
  PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
  PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
]);

// ─────────────────────────────────────────────
// INPUT
// ─────────────────────────────────────────────

$minYear = isset($_GET['min']) ? (int)$_GET['min'] : 2020;
$maxYear = isset($_GET['max']) ? (int)$_GET['max'] : (int)date('Y');

// ─────────────────────────────────────────────
// QUERY
// ─────────────────────────────────────────────

$stmt = $db->prepare("
  SELECT *
  FROM works
  WHERE publication_year BETWEEN :minYear AND :maxYear
  ORDER BY publication_year DESC, cited_by_count DESC
");

$stmt->bindValue(':minYear', $minYear, PDO::PARAM_INT);
$stmt->bindValue(':maxYear', $maxYear, PDO::PARAM_INT);
$stmt->execute();

// ─────────────────────────────────────────────
// STREAM JSON OUTPUT
// ─────────────────────────────────────────────

ob_clean();
echo '{"results":[';

$first = true;
$count = 0;

while ($row = $stmt->fetch()) {

  $work = [
    'id'               => $row['id'] ?? null,
    'title'            => $row['title'] ?? null,
    'publication_year' => isset($row['publication_year']) ? (int)$row['publication_year'] : null,
    'publication_date' => $row['publication_date'] ?? null,
    'cited_by_count'   => (int)($row['cited_by_count'] ?? 0),
    'type'             => $row['type'] ?? null,
    'doi'              => $row['doi'] ?? null,

    'open_access' => [
      'is_oa'     => !empty($row['is_oa']),
      'oa_status' => $row['oa_status'] ?? null
    ],

    'primary_location' => [
      'source' => [
        'display_name'           => $row['journal'] ?? null,
        'host_organization_name' => $row['publisher'] ?? null
      ]
    ],

    'primary_topic' => [
      'display_name' => $row['primary_topic'] ?? null
    ]
  ];

  // APC info
  if (!empty($row['apc_value'])) {
    $work['apc_list'] = [
      'value_usd' => (float)$row['apc_value'],
      'currency'  => $row['apc_currency'] ?? 'USD'
    ];
  }

  // Authors (JSON array of names → OpenAlex-like structure)
  if (!empty($row['authors'])) {
    $authors = json_decode($row['authors'], true);
    if (is_array($authors)) {
      $work['authorships'] = array_map(
        fn($name) => [
          'author' => ['display_name' => $name],
          'institutions' => [
            ['ror' => 'https://ror.org/05ect4e57']
          ]
        ],
        $authors
      );
    }
  }

  // ✅ FUNDERS (JSON array of strings → frontend-compatible)
  if (!empty($row['funders'])) {
    $funders = json_decode($row['funders'], true);
    if (is_array($funders)) {
      $work['funders'] = $funders;
    }
  }

  if (!$first) echo ',';
  echo json_encode($work, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);

  $first = false;
  $count++;
}

echo '],';

// ─────────────────────────────────────────────
// METADATA
// ─────────────────────────────────────────────

$meta = [];
if (file_exists($META_FILE)) {
  $meta = json_decode(file_get_contents($META_FILE), true) ?: [];
}

echo '"meta":{';
echo '"count":' . $count . ',';
echo '"last_updated":' . json_encode($meta['last_updated'] ?? null);
echo '}}';
