// 用 sql.js 直接跑一遍 dashboard.html 里的所有 SQL，验证语法 + schema
// 用法：bun run test_queries.mjs [path/to/manifest.db]
import initSqlJs from "sql.js";
import { readFileSync } from "node:fs";

const SQL = await initSqlJs();
const dbPath = process.argv[2] || "examples/sample_manifest.db";
const buf = readFileSync(dbPath);
const db = new SQL.Database(new Uint8Array(buf));

function q(sql, params = []) {
  const stmt = db.prepare(sql);
  stmt.bind(params);
  const rows = [];
  while (stmt.step()) rows.push(stmt.getAsObject());
  stmt.free();
  return rows;
}
function q1(sql, params = []) { return q(sql, params)[0] || null; }

console.log("=== 1. 探针 ===");
console.log("tables:", q("SELECT name FROM sqlite_master WHERE type='table'").map(r => r.name));

console.log("\n=== 2. overview 统计 ===");
console.log("files total:", q1("SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM files"));
console.log("distinct ref imgs:", q1("SELECT COUNT(DISTINCT image_filename) c FROM refs"));
console.log("distinct md:", q1("SELECT COUNT(DISTINCT md_path) c FROM refs"));
console.log("ref total:", q1("SELECT COUNT(*) c FROM refs"));
console.log("orphan size:", q1(`SELECT COALESCE(SUM(size),0) s FROM files f
  WHERE NOT EXISTS (SELECT 1 FROM refs r WHERE r.image_filename=f.filename)`));

console.log("\n=== 3. 引用量分布 ===");
const buckets = q(`SELECT CASE WHEN rc=1 THEN 0 WHEN rc=2 THEN 1 WHEN rc<=5 THEN 2 WHEN rc<=10 THEN 3 ELSE 4 END b, COUNT(*) c
  FROM (SELECT filename, ref_count rc FROM ref_summary WHERE ref_count > 0) GROUP BY b ORDER BY b`);
console.log(buckets);

console.log("\n=== 4. 默认搜索（前 100） ===");
const search = q(`SELECT f.filename, f.size, f.oss_key,
  (SELECT COUNT(*) FROM refs r WHERE r.image_filename=f.filename) ref_count
  FROM files f ORDER BY ref_count DESC, f.size DESC LIMIT 100`);
console.log("returned:", search.length, "首条:", { filename: search[0].filename, oss_key: search[0].oss_key, ref_count: search[0].ref_count });

console.log("\n=== 5. 模糊搜索 Pasted ===");
const s2 = q(`SELECT f.filename, f.size, f.oss_key,
  (SELECT COUNT(*) FROM refs r WHERE r.image_filename=f.filename) ref_count
  FROM files f WHERE f.filename LIKE ? COLLATE NOCASE
  ORDER BY ref_count DESC, f.size DESC LIMIT 500`, ["%Pasted%"]);
console.log("matches:", s2.length);

console.log("\n=== 6. top ref ===");
console.log(q(`SELECT filename, oss_key, ref_count FROM ref_summary ORDER BY ref_count DESC, filename LIMIT 3`));

console.log("\n=== 7. top size ===");
console.log(q(`SELECT filename, oss_key, size FROM files ORDER BY size DESC LIMIT 3`));

console.log("\n=== 8. orphans ===");
const orph = q(`SELECT f.filename, f.size, f.oss_key FROM files f
  WHERE NOT EXISTS (SELECT 1 FROM refs r WHERE r.image_filename=f.filename)
  ORDER BY f.size DESC`);
console.log("orphan count:", orph.length, "first:", orph[0]?.filename);

console.log("\n=== 9. detail (refs for a known image) ===");
const sample = search.find(r => r.ref_count > 0);
if (sample) {
  console.log(`refs for ${sample.filename}:`, q("SELECT md_path FROM refs WHERE image_filename=? ORDER BY md_path", [sample.filename]).slice(0,3));
}

console.log("\n✅ 全部 SQL 通过");
