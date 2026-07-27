# VERITAS Compatibility Assessment: fact_extractor

**Summary:** 0 FAIL, 0 WARN, 6 PASS, 1 INFO

✅ **No FAIL/WARN results — this candidate looks like a straightforward fit.**

## ✅ Single-container buildability — PASS
Exactly one Dockerfile found: Dockerfile

## ✅ Multi-service architecture — PASS
No docker-compose file found — candidate likely runs as a single container.

## ✅ Bare-host / Docker-in-Docker assumptions — PASS
No references to docker.sock or nested docker commands found in scripts.

## ✅ Architecture-hardcoding — PASS
No hardcoded amd64/x86_64 references found without architecture detection.

## ℹ️ Privileged / device requirements — INFO
No privileged mode or device mount requirements detected.

## ✅ Install-time reboot requirement — PASS
No documentation references a required reboot during installation.

## ✅ Maintenance signal — PASS
Last pushed: 2026-07-01T13:14:31Z. Open issues: 42. Not archived.
