<?php
// PHP CLI 示例：没有 index.php 时按 CLI 跑（php 脚本.php），跑完状态自动变 exited
echo "cli start, port=" . (getenv("PORT") ?: "-") . ", job=" . (getenv("JOB_NAME") ?: "-") . "\n";

for ($i = 1; $i <= 3; $i++) {
    echo "step {$i}/3\n";
    flush();                     // 面板实时看到输出
    sleep(1);
}

echo "done\n";
