<?php
// PHP Web 示例：面板检测到 index.php 会自动用 `php -S 0.0.0.0:$PORT -t <目录>` 启动
header("Content-Type: text/plain; charset=utf-8");

$host = getenv("HOST") ?: "0.0.0.0";
$port = getenv("PORT") ?: "-";

echo "hello from php web\n";
echo "listen {$host}:{$port}\n";
echo "uri " . ($_SERVER["REQUEST_URI"] ?? "/") . "\n";
echo "PHP " . PHP_VERSION . " | job " . (getenv("JOB_NAME") ?: "-") . "\n";
echo "time " . date("Y-m-d H:i:s") . "\n";
