% Solution checker for `model.mzn` (Australia map colouring).
%
% An *output-style* checker: it re-declares each region as a plain `int`
% parameter — MiniZinc binds these to the values from the solution being
% checked — and re-states the nine border constraints independently of the
% model. If every border holds it prints `CORRECT`, otherwise `INCORRECT`.
% The verdict is rendered text the server surfaces verbatim; an output-style
% checker reports its judgement this way rather than as a machine-readable
% violation (that is the constraint-style idiom).
%
% Adapted from the MiniZinc documentation:
% https://docs.minizinc.dev/en/stable/checkers.html

int: wa;
int: nt;
int: sa;
int: q;
int: nsw;
int: v;
int: t;

output [
  if wa!=nt /\ wa!=sa /\ nt!=sa /\ nt!=q /\ sa!=q /\
     sa!=nsw /\ sa!=v /\ q!=nsw /\ nsw!=v
  then "CORRECT\n"
  else "INCORRECT\n"
  endif
];