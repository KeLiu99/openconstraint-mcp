int: n_jobs;
int: n_workers;

set of int: Jobs = 1..n_jobs;
set of int: Workers = 1..n_workers;

array[Jobs] of int: duration;
array[Jobs] of var Workers: assigned_to;
array[Workers] of var 0..sum(duration): load;

constraint forall(w in Workers) (
    load[w] = sum(j in Jobs)(duration[j] * bool2int(assigned_to[j] = w))
);

var 0..sum(duration): max_load;

constraint max_load = max(load);

solve minimize max_load;

output [
    "max_load=\(max_load)\n",
    "loads=\(load)\n",
    "assigned_to=\(assigned_to)\n"
];
