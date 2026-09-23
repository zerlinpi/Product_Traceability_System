"""HTTP blueprints, one module per domain.

Each module declares a ``Blueprint`` and its routes at module level. They are
registered by ``create_app`` in ``app.py``.

The point of the split is navigability: ``app.py`` had every route defined
inside ``create_app()``, so finding one meant searching an 8.6k-line file and
every route reached its helpers through a closure. A blueprint declares its own
dependencies at the top of the file, which is both easier to read and the reason
the shared foundation (``errors``, ``responses``, ``validators``,
``serializers``) was extracted first.

Rules for a module in here:

* It must not import ``app``. The dependency runs one way — ``app`` imports the
  blueprint — and a cycle would break startup.
* It must not read ``app.config`` directly. Use ``flask.current_app`` if a
  setting is genuinely needed, so the module stays testable without an app.
* Permission is enforced by the global gate in ``traceability/auth.py`` plus the
  same ``require_*`` decorators the routes used before the move. Moving a route
  must never change who can call it; ``tools/extract_routes.py --check`` compares
  the generated permission matrix on every build and will fail if it does.
"""
