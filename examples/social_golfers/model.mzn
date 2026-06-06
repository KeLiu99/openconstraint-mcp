% Social Golfer Problem (SGP).
%
% Schedule n_groups * group_size golfers into n_groups groups of group_size
% golfers, once per week for n_weeks weeks, so that no two golfers ever share a
% group more than once ("maximum socialisation": no repeated pairs).
%
% "As many weeks as possible" is this same satisfaction problem re-solved with
% n_weeks raised until it becomes unsatisfiable. The classic 15-golfer instance
% (5 groups of 3) tops out at 7 weeks -- Kirkman's schoolgirls, where all 105
% pairs meet exactly once ("full socialisation"); 8 weeks is infeasible.

include "globals.mzn";

int: n_groups;                  % groups playing each week (m)
int: group_size;                % golfers per group (n)
int: n_weeks;                   % weeks to schedule (p)

int: n_golfers = n_groups * group_size;

set of int: GROUP  = 1..n_groups;
set of int: GOLFER = 1..n_golfers;
set of int: WEEK   = 1..n_weeks;

% group[w, p] = the group golfer p plays in during week w.
array[WEEK, GOLFER] of var GROUP: group;

% Every group, every week, holds exactly group_size golfers.
constraint forall(w in WEEK, g in GROUP)(
    sum(p in GOLFER)(bool2int(group[w, p] = g)) = group_size
);

% No pair of golfers shares a group in more than one week.
constraint forall(p1, p2 in GOLFER where p1 < p2)(
    sum(w in WEEK)(bool2int(group[w, p1] = group[w, p2])) <= 1
);

% --- Symmetry breaking (the schedule is highly symmetric; this is what keeps
% the search tractable and makes enumerated solutions meaningfully distinct) ---

% Week 1 is fixed to the "natural" partition: golfers 1..group_size in group 1,
% the next group_size in group 2, and so on.
constraint forall(p in GOLFER)(
    group[1, p] = (p - 1) div group_size + 1
);

% Break the per-week group-relabelling symmetry: within each week, group label k
% must first appear (in golfer order) before label k+1. This also pins golfer 1
% to group 1 every week, since golfer 1 is the first index.
constraint forall(w in WEEK)(
    value_precede_chain([g | g in GROUP], [group[w, p] | p in GOLFER])
);

solve satisfy;

% One line per week, listing each group's golfers.
output [
    "Week \(w): " ++
    concat(
        [ "[" ++ join(" ", [ "\(p)" | p in GOLFER where fix(group[w, p]) = g ]) ++ "] "
        | g in GROUP ]
    ) ++ "\n"
    | w in WEEK
];
