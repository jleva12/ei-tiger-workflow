# Real-infrastructure verification

These tests are intentionally excluded from infrastructure claims made by the
ordinary unit suite. They use a real MongoDB: the lease checks run against any
server, the transaction checks need a replica set or mongos.

Install the profile:

```bash
pip install -e ".[dev,integration]"
```

Run the Mongo transaction and lease checks (replica set):

```bash
ETF_REAL_MONGO_URI='mongodb://localhost:27017/?replicaSet=rs0' \
pytest -m real_infrastructure tests/integration/test_real_mongo.py
```

Run only the lease checks, which a standalone `mongod` also supports:

```bash
ETF_REAL_MONGO_URI='mongodb://localhost:27017' \
pytest -m real_infrastructure tests/integration/test_real_mongo.py -k lock_provider
```

The tests skip with a setup message when their environment variable is absent.
Once it is supplied, an unreachable or incorrectly configured service is a
failure rather than a skip. Use isolated, disposable databases because the tests
create and clean up uniquely named probe data.
