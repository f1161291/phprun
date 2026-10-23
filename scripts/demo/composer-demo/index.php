<?php
// composer 示例：首次启动面板会先执行 composer install（日志有 [composer] 标记），
// 之后每次请求都从 vendor/autoload.php 加载依赖。
require __DIR__ . "/vendor/autoload.php";

use Monolog\Logger;
use Monolog\Handler\StreamHandler;

$log = new Logger("composer-demo");
$log->pushHandler(new StreamHandler("php://stdout", Logger::INFO));
$log->info("monolog 已加载，PHP " . PHP_VERSION);

header("Content-Type: text/plain; charset=utf-8");
echo "composer demo ok, port=" . (getenv("PORT") ?: "-") . "\n";
echo "vendor/autoload.php loaded\n";
