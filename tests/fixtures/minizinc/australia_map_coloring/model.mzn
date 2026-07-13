% Australia map colouring — the classic graph-colouring constraint problem.
%
% Colour each of Australia's seven states/territories with one of `nc` colours
% so that no two regions that share a border get the same colour. The regions
% are Western Australia (wa), Northern Territory (nt), South Australia (sa),
% Queensland (q), New South Wales (nsw), Victoria (v) and Tasmania (t); every
% `!=` constraint below is one shared land border. Tasmania is an island (no
% land borders), so `t` is unconstrained and may take any colour.
%
% This example ships a solution checker, `model.mzc.mzn`, that independently
% re-verifies every border constraint. It is here to demonstrate the checker
% feature: pass the checker as `checker_path` to `solve_minizinc_files` (or
% `checker` to `solve_minizinc_model`) and the result's `checker` report will
% confirm the solver's colouring satisfies the model. The data (`nc = 3`) is
% inline, so this example has no `data.dzn`.
%
% Adapted from the MiniZinc documentation:
% https://docs.minizinc.dev/en/stable/checkers.html

int: nc = 3;

var 1..nc: wa;   var 1..nc: nt;  var 1..nc: sa;   var 1..nc: q;
var 1..nc: nsw;  var 1..nc: v;   var 1..nc: t;

constraint wa != nt;
constraint wa != sa;
constraint nt != sa;
constraint nt != q;
constraint sa != q;
constraint sa != nsw;
constraint sa != v;
constraint q != nsw;
constraint nsw != v;
solve satisfy;

output ["wa=\(wa)\t nt=\(nt)\t sa=\(sa)\n",
        "q=\(q)\t nsw=\(nsw)\t v=\(v)\n",
         "t=", show(t),  "\n"];