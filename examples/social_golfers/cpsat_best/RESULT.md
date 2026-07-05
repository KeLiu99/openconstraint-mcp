# CP-SAT best result

Instance: social golfers `7-3-10` using the compact Fano-plane CP-SAT formulation.

Saved configuration:

```json
{
  "seed": 7,
  "num_workers": 8,
  "search_branching": "PORTFOLIO_SEARCH",
  "randomize_search": true,
  "use_lns": true,
  "diversify_lns_params": false,
  "symmetry_level": 2
}
```

MCP sweep results:

| attempt | status | checker | duration |
| --- | --- | --- | ---: |
| `portfolio_8w_seed7` | optimal | accepted | 552 ms |
| `automatic_8w_seed21` | optimal | accepted | 558 ms |
| `quick_restart_8w_seed21` | optimal | accepted | 605 ms |
| `fixed_1w_seed1` | optimal | accepted | 30,786 ms |

The saved replay was re-run through `save_verified_cpsat_python` and accepted by
`checker.py`: 10 weeks, 210 unique pairs, no repeated pair. The final save-time
verification run took 452 ms.
