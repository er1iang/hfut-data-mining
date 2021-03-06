# -*- coding:utf-8 -*-
from __future__ import unicode_literals

from gevent.lock import Semaphore
from pymongo import InsertOne

from .db import report_db, InsertOnNotExist
from .log import logger
from .util import term_range, elapse_dec


class Spider:
    def __init__(self, student_shortcut, job_manager, db_manager):
        self.shortcut = student_shortcut
        self.job_manager = job_manager
        self.db_manager = db_manager

        self.term_course_mutex = Semaphore()
        self.term_course_flags = set()

        # 避免重复写数据库
        # 学期, 专业, 计划, 班级学生关系记录 不会重复抓取
        # 课程记录使用 课程代码 区分
        # 课程学期教学班使用 学期代码 + 课程代码 区分
        # 学生记录使用 学号 区分
        # 三者不会有相同情况, 只用一个集合即可
        # 在这里没有使用锁, 因此这个标记不一定会命中
        # 使用锁会增加很多等待时间, 还不如直接更新数据库
        # 并没有提升多少性能
        # self._flags = {
        #     'course': ('课程代码', Semaphore(), set()),
        #     'student': ('学号', Semaphore(), set()),
        # }
        # 统计信息: 内存标志命中次数
        # self._flags_hit_count = {
        #     'course': 0,
        #     'student': 0,
        # }

    @elapse_dec
    def crawl(self, dfs_mode=False):
        self.job_manager.jobs = (
            self.iter_term_and_major,
            self.iter_term_and_course,
            self.iter_teaching_class,
            self.sync_students
        )
        logger.info('Crawl start!'.center(72, '='))
        self.job_manager.start(dfs_mode)
        logger.info('Jobs are all dispatched. Waiting for database requests handling.')
        self.db_manager.join()
        logger.info('Crawl finished!'.center(72, '='))
        report_db(self.db_manager.db)

    # 以下是任务
    def iter_term_and_major(self):
        # @structure {'专业': [{'专业代码': str, '专业名称': str}], '学期': [{'学期代码': str, '学期名称': str}]}
        code = self.shortcut.get_code()
        terms = code['学期']
        majors = code['专业']

        for term in terms:
            term_code = term['学期代码']
            self.db_manager.request('term', InsertOnNotExist({'学期代码': term_code}, term))
            yield term_code, None

        max_term_number = int(terms[-1]['学期代码'])
        # 一些专业被删掉了, 因此很多记录都没了= =
        for major in majors:
            major_code = major['专业代码']
            self.db_manager.request('major', InsertOnNotExist({'专业代码': major_code}, major))
            for i in term_range(major['专业名称'], max_term_number):
                term_code = '%03d' % i
                yield term_code, major_code

    def iter_term_and_course(self, term_code, major_code=None):
        if major_code:
            courses = self.shortcut.get_teaching_plan(xqdm=term_code, zydm=major_code)
            for course in courses:
                course_code = course['课程代码']
                self.db_manager.request('course', InsertOnNotExist({'课程代码': course_code}, course))

                plan_doc = {'课程代码': course_code, '学期代码': term_code, '专业代码': major_code}
                self.db_manager.request('plan', InsertOnNotExist(plan_doc, plan_doc))

                yield term_code, course_code
        else:
            courses = self.shortcut.get_teaching_plan(xqdm=term_code, kclx='x')
            for course in courses:
                course_code = course['课程代码']
                self.db_manager.request('course', InsertOnNotExist({'课程代码': course_code}, course))
                yield term_code, course_code

    def iter_teaching_class(self, term_code, course_code=None, course_name=None):
        if course_code is None:
            is_new = True
        else:
            key = term_code + course_code
            self.term_course_mutex.acquire()
            is_new = key not in self.term_course_flags
            if is_new:
                self.term_course_flags.add(key)
            # 在 if 内释放锁会导致出现重复键时锁无法释放
            self.term_course_mutex.release()

        if is_new:
            # @structure [{'任课教师': str, '课程名称': str, '教学班号': str, 'c': str, '班级容量': int}]
            classes = self.shortcut.search_course(xqdm=term_code, kcdm=course_code, kcmc=course_name)
            for teaching_class in classes:
                course_code = teaching_class['课程代码']
                class_code = teaching_class['教学班号']
                # @structure {'校区': str,'开课单位': str,'考核类型': str,'课程类型': str,'课程名称': str,'教学班号': str,
                # '起止周': str, '时间地点': str,'学分': float,'性别限制': str,'优选范围': str,'禁选范围': str,'选中人数': int,'备 注': str}
                class_info = self.db_manager.db['class'].find_one(
                    {'学期代码': term_code, '课程代码': course_code, '教学班号': class_code}
                )
                if not class_info:
                    class_info = self.shortcut.get_class_info(
                        xqdm=term_code, kcdm=course_code, jxbh=class_code
                    )
                    class_info.update(teaching_class)
                    # 接口没有学期代码参数
                    class_info['学期代码'] = term_code

                    self.db_manager.request('class', InsertOne(class_info))
                yield term_code, course_code, class_code

    def sync_students(self, term_code, course_code, class_code):
        # @structure {'学期': str, '班级名称': str, '学生': [{'姓名': str, '学号': int}]}
        students = self.shortcut.get_class_students(xqdm=term_code, kcdm=course_code, jxbh=class_code)
        # 可能没有结果
        if students:
            students = students['学生']
            for student in students:
                student_code = student['学号']
                student_name = student['姓名']
                student['性别'] = '女' if student_name.endswith('*') else '男'
                student['姓名'] = student_name.rstrip('*')

                self.db_manager.request('student', InsertOnNotExist({'学号': student_code}, student))

                class_student_doc = {'学期代码': term_code, '课程代码': course_code, '教学班号': class_code, '学号': student_code}
                self.db_manager.request('class_student', InsertOnNotExist(class_student_doc, class_student_doc))
