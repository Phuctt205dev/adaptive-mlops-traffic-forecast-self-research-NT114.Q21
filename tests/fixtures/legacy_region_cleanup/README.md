# Frozen legacy source fixture

Captured read-only on 07 November 2026 from the application source ConfigMap
`traffic-region-cleanup-18054892a943`. These are source files and their existing
manifest, not a snapshot of application data, environment values or Secrets.

The isolated Kubernetes test mounts these files at `/opt/region-cleanup` in the
**actual pinned legacy image** so `cache_sync.load_uploader()` can reproduce the
same verified import hooks used in production. Without this fixture, importing
the correct uploader file still misses its original module context.

Do not edit these Python files or update manifest hashes to make a test pass.
A different legacy baseline needs a separately reviewed migration fixture.
This directory is never included in the role images' runtime import closure.
