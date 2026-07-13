% Bounded knapsack problem.
%
% Given n item types, each with a weight, a value, and a maximum available
% count, choose how many of each to pack so the total weight stays within the
% knapsack capacity and the total value is maximised.

int: n;
set of int: ITEM = 1..n;

array[ITEM] of int: weight;     % weight (mass) of one unit of each item
array[ITEM] of int: value;      % value of one unit of each item
array[ITEM] of int: max_count;  % how many units of each item are available
int: capacity;                  % knapsack weight limit

% Decision: how many units of each item to pack (0 .. its available count).
array[ITEM] of var int: take;
constraint forall(i in ITEM)(take[i] in 0..max_count[i]);

var int: total_weight = sum(i in ITEM)(take[i] * weight[i]);
var int: total_value  = sum(i in ITEM)(take[i] * value[i]);

% Stay within the knapsack.
constraint total_weight <= capacity;

solve maximize total_value;

output
  [ "Items packed:\n" ] ++
  [ "  item \(i): \(take[i]) × (w=\(weight[i]), v=\(value[i]))\n"
    | i in ITEM where fix(take[i]) > 0 ] ++
  [ "Total weight: \(total_weight) / \(capacity)\n",
    "Total value:  \(total_value)\n" ];
