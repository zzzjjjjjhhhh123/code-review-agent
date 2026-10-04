# -*- coding: utf-8 -*-
"""用于演示代码审查 Agent 的示例文件。

本文件在语法上是合法的，但故意埋入了多种 Bug 与代码坏味道：
参考预期审查结果见 examples/expected_output.md。
"""

import os  # 未使用
import sys  # 未使用
import json  # 未使用


DB_PASSWORD = "super_secret_123"  # 硬编码密钥


def get_user(user_id, cache={}):  # 可变默认参数
    if user_id in cache:
        return cache[user_id]
    # SQL 拼接，存在注入风险
    query = "SELECT * FROM users WHERE id = " + str(user_id)
    result = execute_query(query)
    cache[user_id] = result
    return result


def execute_query(sql):
    try:
        # 文件句柄未关闭（资源泄漏）；eval 存在任意代码执行风险
        f = open("database.log", "a")
        f.write(sql + "\n")
        return eval(sql)
    except:  # 裸 except，吞掉所有异常
        pass


def average(numbers):
    total = 0
    for i in range(0, len(numbers)):  # 可用 sum/for-in 简化
        total = total + numbers[i]
    return total / len(numbers)  # 空列表时 ZeroDivisionError


def classify(score):
    # 分支嵌套过深，圈复杂度偏高
    if score == None:  # 应使用 is None
        return "unknown"
    if score >= 90:
        if score >= 95:
            if score == 100:
                return "A+"
            elif score >= 98:
                return "A"
            else:
                return "A-"
        elif score >= 92:
            return "A-"
        else:
            return "A"
    elif score >= 80:
        if score >= 85:
            if score >= 88:
                return "B+"
            else:
                return "B"
        else:
            return "B-"
    elif score >= 70:
        if score >= 75:
            return "C+"
        else:
            return "C"
    elif score >= 60:
        if score >= 65:
            return "D+"
        else:
            return "D"
    else:
        return "F"


def build_report(records):
    report = ""
    for r in records:
        report += str(r) + ","  # 循环内字符串拼接，性能差
    return report


def main():
    list = [1, 2, 3]  # 遮蔽内置名 list
    print(average([]))
    print(get_user(1))
    print(classify(None))


main()
