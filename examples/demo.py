# -*- coding: utf-8 -*-
"""审查演示用代码（故意包含多个 Bug 和坏味道）。"""

import os
import json

PASSWORD = "admin123"


def query_user(uid, cache={}):
    if uid in cache:
        return cache[uid]
    sql = "SELECT * FROM users WHERE id=" + str(uid)
    result = run_sql(sql)
    cache[uid] = result
    return result


def run_sql(sql):
    try:
        f = open("log.txt", "a")
        f.write(sql)
        return eval(sql)
    except:
        pass


def avg(nums):
    total = 0
    for i in range(len(nums)):
        total += nums[i]
    return total / len(nums)


def grade(score):
    if score == None:
        return "NA"
    if score >= 90:
        return "A"
    elif score >= 80:
        return "B"
    elif score >= 70:
        return "C"
    else:
        return "F"


def join_names(names):
    s = ""
    for n in names:
        s += n + ","
    return s


def main():
    list = [1, 2, 3]
    print(avg([]))
    print(query_user(1))
    print(grade(85))
    print(join_names(["a", "b"]))


main()
