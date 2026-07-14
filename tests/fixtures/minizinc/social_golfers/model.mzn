% Social Golfer Problem (SGP) — alldifferent encoding + strong week-2 symmetry break.
%
% Key insight from Liu, Löffler, Hofstedt (2019): fixing week 2 with a
% transversal cross-cutting of week-1 groups dramatically reduces the
% search space for instances of the form g-s-w where g is a multiple of s:
% groups in week 2 take one golfer from each of the first s week-1 groups
% (and similarly for subsequent blocks of s groups). E.g. for 6-3-8 (g=6, s=3):
%
% Week 1: natural partition (1,2,3)(4,5,6)(7,8,9)(10,11,12)(13,14,15)(16,17,18)
% Week 2: transversal: (1,4,7)(2,5,8)(3,6,9)(10,13,16)(11,14,17)(12,15,18)
%
% This is valid symmetry breaking because any schedule can be relabelled
% (permuting group labels in week 2) to satisfy this pattern. The shipped
% data.dzn (5-3-7) has g=5, s=3 — g is not a multiple of s, so the `if` guard
% below is false and only the weaker per-week ordering breaks are active.

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

% Week 2: transversal cross-cutting of week-1 groups (Liu, Löffler, Hofstedt 2019).
% Only valid when n_groups is a multiple of group_size — otherwise the leftover
% groups create an interaction that eliminates all valid completions past week 2.
constraint if n_groups mod group_size == 0 then
    forall(b in 0..(n_groups div group_size - 1))(
        let {
            int: block_start = b * group_size
        } in
        forall(g in 1..group_size, s in 1..group_size)(
            schedule[2, block_start + g, s] =
                (block_start + s - 1) * group_size + g
        )
    )
endif;

% Symmetry: groups ordered by smallest member within each week (weeks 2+)
constraint forall(w in WEEK where w > 1)(
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

solve :: int_search(
    [schedule[w, g, s] | w in WEEK where w > 1, g in GROUP, s in 1..group_size],
    first_fail, indomain_min, complete)
satisfy;

output [
    "Week \(w): " ++
    concat([
        "[" ++ join(" ", [ "\(schedule[w, g, s])" | s in 1..group_size ]) ++ "] "
        | g in GROUP
    ]) ++ "\n"
    | w in WEEK
];
