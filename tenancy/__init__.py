"""Multi-tenant control center (ADR-0011): operator keys, credits, leases.

A separate Flask service that fronts the engine: tenants register, hold
credits, and spawn tunnels on the same host — everything local the Control API
already guards by loopback binding.
"""
