% Nonogram puzzle.
%
% Shade cells in an n_rows by n_cols grid so every row and column matches its
% ordered block clues. A clue such as [3, 1, 2] means a run of 3 shaded cells,
% at least one blank cell, then a run of 1, at least one blank cell, then a run
% of 2. Unused entries in row_len and col_len are 0.

int: n_rows;
int: n_cols;
int: max_row_blocks;
int: max_col_blocks;

set of int: ROW = 1..n_rows;
set of int: COL = 1..n_cols;
set of int: ROW_BLOCK = 1..max_row_blocks;
set of int: COL_BLOCK = 1..max_col_blocks;

array[ROW] of int: row_count;
array[ROW, ROW_BLOCK] of int: row_len;
array[COL] of int: col_count;
array[COL, COL_BLOCK] of int: col_len;

array[ROW, COL] of var bool: shaded;

array[ROW, ROW_BLOCK] of var 0..n_cols: row_start;
array[COL, COL_BLOCK] of var 0..n_rows: col_start;

% Data sanity: used clues are positive, unused clues are 0, and each line has
% enough space for its blocks plus the mandatory one-cell gaps between blocks.
constraint forall(r in ROW)(
    row_count[r] >= 0 /\ row_count[r] <= max_row_blocks /\
    sum(b in ROW_BLOCK where b <= row_count[r])(row_len[r, b]) + max(0, row_count[r] - 1) <= n_cols
);

constraint forall(c in COL)(
    col_count[c] >= 0 /\ col_count[c] <= max_col_blocks /\
    sum(b in COL_BLOCK where b <= col_count[c])(col_len[c, b]) + max(0, col_count[c] - 1) <= n_rows
);

constraint forall(r in ROW, b in ROW_BLOCK)(
    if b <= row_count[r] then
        row_len[r, b] > 0 /\ row_start[r, b] >= 1 /\ row_start[r, b] <= n_cols - row_len[r, b] + 1
    else
        row_len[r, b] = 0 /\ row_start[r, b] = 0
    endif
);

constraint forall(c in COL, b in COL_BLOCK)(
    if b <= col_count[c] then
        col_len[c, b] > 0 /\ col_start[c, b] >= 1 /\ col_start[c, b] <= n_rows - col_len[c, b] + 1
    else
        col_len[c, b] = 0 /\ col_start[c, b] = 0
    endif
);

% Consecutive blocks in a line must be separated by at least one blank cell.
constraint forall(r in ROW, b in ROW_BLOCK where b < row_count[r])(
    row_start[r, b] + row_len[r, b] < row_start[r, b + 1]
);

constraint forall(c in COL, b in COL_BLOCK where b < col_count[c])(
    col_start[c, b] + col_len[c, b] < col_start[c, b + 1]
);

% A cell is shaded exactly when it lies inside one of the row's clue blocks.
constraint forall(r in ROW, c in COL)(
    shaded[r, c] = exists(b in ROW_BLOCK where b <= row_count[r])(
        row_start[r, b] <= c /\ c < row_start[r, b] + row_len[r, b]
    )
);

% The same shaded grid must also match every column's clue blocks.
constraint forall(r in ROW, c in COL)(
    shaded[r, c] = exists(b in COL_BLOCK where b <= col_count[c])(
        col_start[c, b] <= r /\ r < col_start[c, b] + col_len[c, b]
    )
);

solve satisfy;

output [
    if fix(shaded[r, c]) then "#" else "." endif ++
    if c = n_cols then "\n" else "" endif
    | r in ROW, c in COL
];
