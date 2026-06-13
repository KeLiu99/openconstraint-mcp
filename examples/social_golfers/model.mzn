% Social Golfer Problem (SGP) — alldifferent encoding + strong week-2 symmetry break.
%
% Key insight from Liu, Löffler, Hofstedt (2019): fixing week 2 with a
% transversal cross-cutting of week-1 groups dramatically reduces the
% search space for instances of the form g-s-w where g is a multiple of s.
% For 6-3-8, this means: groups in week 2 take one golfer from each of
% the first s week-1 groups (and similarly for subsequent blocks of s groups).
%
% Week 1: natural partition (1,2,3)(4,5,6)(7,8,9)(10,11,12)(13,14,15)(16,17,18)
% Week 2: transversal: (1,4,7)(2,5,8)(3,6,9)(10,13,16)(11,14,17)(12,15,18)
%
% This is valid symmetry breaking because any schedule can be relabelled
% (permuting group labels in week 2) to satisfy this pattern.

include "globals.mzn";

int: n_groups;
int: group_size;
int: n_weeks;

int: n_golfers = n_groups * group_size;

set of int: GROUP  = 1..n_groups;
set of int: GOLFER = 1..n_golfers;
set of int: WEEK   = 1..n_weeks;

% schedule[w, g, s] = the s-th golfer in group g during week w.
array[WEEK, GROUP, 1..group_size] of var GOLFER: schedule;

% Each week is a permutation of all golfers (each golfer exactly once)
constraint forall(w in WEEK)(
    alldifferent([schedule[w, g, s] | g in GROUP, s in 1..group_size])
);

% Within each group, golfers are sorted (break intra-group permutation)
constraint forall(w in WEEK, g in GROUP)(
    increasing([schedule[w, g, s] | s in 1..group_size])
);

% Week 1: natural partition
constraint forall(g in GROUP, s in 1..group_size)(
    schedule[1, g, s] = (g - 1) * group_size + s
);

% Week 2: transversal cross-cutting of week-1 groups.
% Partition the n_groups into blocks of group_size groups each.
% Within each block of group_size groups, week-2 group g takes the g-th
% golfer from EACH of the group_size week-1 groups in that block.
% For block b (0-indexed), week-1 groups are indices (b*group_size + s)
% and their g-th golfer is (b*group_size + s - 1)*group_size + g.
constraint forall(b in 0..(n_groups div group_size - 1))(
    let {
        int: block_start = b * group_size
    } in
    forall(g in 1..group_size, s in 1..group_size)(
        schedule[2, block_start + g, s] =
            (block_start + s - 1) * group_size + g
    )
);

% Symmetry: groups ordered by smallest member within each week (weeks 3+)
constraint forall(w in WEEK where w > 2)(
    forall(g in 2..n_groups)(
        schedule[w, g-1, 1] < schedule[w, g, 1]
    )
);

% Derive: which group does golfer p play in during week w?
array[WEEK, GOLFER] of var GROUP: golfer_group;

constraint forall(w in WEEK, g in GROUP, s in 1..group_size)(
    golfer_group[w, schedule[w, g, s]] = g
);

% No pair of golfers shares a group in more than one week.
constraint forall(p1, p2 in GOLFER where p1 < p2)(
    sum(w in WEEK)(golfer_group[w, p1] = golfer_group[w, p2]) <= 1
);

solve satisfy;

output [
    "Week \(w): " ++
    concat([
        "[" ++ join(" ", [ "\(schedule[w, g, s])" | s in 1..group_size ]) ++ "] "
        | g in GROUP
    ]) ++ "\n"
    | w in WEEK
];
