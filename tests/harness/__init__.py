"""The test harness: fixtures, cassettes, and cleanup.

Four later steps need this and rev 5 of the plan gave it no owner, so S5 owns
it. Three concerns, three modules, and each carries a decision that is cheaper
to make now than to retrofit:

``repos``
    **Fixture repositories are synthesised, not committed.** S8 wants a
    testcontainers repo, S9 wants Maven/Gradle/Node/Python, S21 wants a real
    one. The alternatives were a separate fixtures repository — which versions
    independently and is stale the first time anybody forgets it exists — or
    committing real repositories here, which means committing Gradle wrappers
    and lockfiles: binaries in the supply chain, and a Dependabot surface for
    code that only ever exists to be probed. What the probe actually reads is
    *which files are present*, so the fixtures build a real git repository at
    test time with the right shape and nothing inside it.

``cassette``
    **Recorded LLM interactions, and a miss is an error.** Without record and
    replay, every test touching an agent step is slow, flaky and billable, and
    CI quietly stops being run. The rule that makes it work is that replay
    never falls through to the network: a cassette miss fails the test and says
    what to re-record. Recording is opt-in through an environment variable, so
    a missing cassette in CI cannot silently start spending money.

``cleanup``
    **Nothing outlives the test that made it.** Tests write worktrees now and
    will spawn containers from S7. Both leak, and a leaked container poisons
    the next run in a way that looks like a flaky test. The ``Reaper`` releases
    in reverse order, keeps going when a release fails, and the session refuses
    to end quietly with anything outstanding.

The fourth guarantee has no module because it is a fixture: ``tests/conftest``
blocks TCP for the whole suite, so "zero network calls" is enforced rather than
asserted.
"""
